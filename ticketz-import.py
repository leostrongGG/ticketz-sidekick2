#!/usr/bin/env python3
"""
Ticketz Import
==============
Import a single company from a filtered backup into an existing Ticketz database.

Reads a db_dump.sql from a filtered backup (produced by ticketz-filter.py),
remaps ALL IDs to avoid conflicts with the target database, and generates
an import SQL file wrapped in a single transaction for atomic safety.

Safety features:
  - Single transaction (BEGIN/COMMIT) — any error causes full ROLLBACK
  - --dry-run mode generates SQL without executing
  - Validates backup has exactly 1 company (besides company 1)
  - Validates target database is NOT empty (else use restore)
  - Pre-import safety backup recommended
  - session_replication_role=replica disables FK triggers during bulk import

Usage (called by sidekick2.sh):
  python3 ticketz-import.py <db_dump.sql> \\
    --db-host HOST --db-name NAME --db-user USER [--db-port PORT] \\
    --output import.sql [--media-map mapping.json] [--dry-run]

Media paths in the dump are remapped automatically:
  media/{oldCompanyId}/{oldContactId}/{oldTicketId}/... →
  media/{newCompanyId}/{newContactId}/{newTicketId}/...

External URLs (http/https) are left unchanged.
"""

import sys
import re
import os
import time
import subprocess
import json
import argparse


# ============================================================
# TABLE CLASSIFICATION (shared with ticketz-filter.py)
# ============================================================

GLOBAL_TABLES = {
    'Plans', 'Helps', 'Translations', 'SequelizeMeta', 'SequelizeData'
}

TABLES_WITH_COMPANY_ID = {
    'Announcements', 'CampaignSettings', 'Campaigns', 'Chats',
    'Contacts', 'ContactListItems', 'ContactLists', 'Counters',
    'Funnels', 'Invoices', 'Messages', 'Queues', 'QuickMessages',
    'QuickPix', 'Schedules', 'Settings', 'Subscriptions', 'Tags',
    'TicketTraking', 'Tickets', 'Users', 'UserRatings', 'Whatsapps',
    'WhatsappLidMaps'
}

# Tables from which we collect IDs in pass 1 (primary entities with FKs)
COLLECT_IDS_FROM = {
    'Whatsapps':    'whatsapp_ids',
    'Chats':        'chat_ids',
    'Contacts':     'contact_ids',
    'Campaigns':    'campaign_ids',
    'Queues':       'queue_ids',
    'Tickets':      'ticket_ids',
    'Users':        'user_ids',
    'Funnels':      'funnel_ids',
    'Tags':         'tag_ids',
    'ContactLists': 'contactlist_ids',
}

INDIRECT_TABLES = {
    'Baileys':                  ('whatsappId',  'whatsapp_ids'),
    'BaileysKeys':              ('whatsappId',  'whatsapp_ids'),
    'CampaignShipping':         ('campaignId',  'campaign_ids'),
    'ChatMessages':             ('chatId',      'chat_ids'),
    'ChatUsers':                ('chatId',      'chat_ids'),
    'ContactCustomFields':      ('contactId',   'contact_ids'),
    'ContactTags':              ('contactId',   'contact_ids'),
    'FunnelSteps':              ('funnelId',    'funnel_ids'),
    'IntegrationSessions':      ('ticketId',    'ticket_ids'),
    'Integrations':             ('queueId',     'queue_ids'),
    'NotificamehubIdMappings':  ('ticketId',    'ticket_ids'),
    'OldMessages':              ('ticketId',    'ticket_ids'),
    'OutOfTicketMessages':      ('whatsappId',  'whatsapp_ids'),
    'QueueOptions':             ('queueId',     'queue_ids'),
    'TicketNotes':              ('ticketId',    'ticket_ids'),
    'TicketTags':               ('ticketId',    'ticket_ids'),
    'UserQueues':               ('userId',      'user_ids'),
    'UserSocketSessions':       ('userId',      'user_ids'),
    'Wavoips':                  ('whatsappId',  'whatsapp_ids'),
    'WebpushSubscriptions':     ('userId',      'user_ids'),
    'WhatsappQueues':           ('whatsappId',  'whatsapp_ids'),
}

# All data tables (excluding global)
ALL_DATA_TABLES = {'Companies'} | TABLES_WITH_COMPANY_ID | set(INDIRECT_TABLES.keys())

ADMIN_COMPANY_ID = '1'


# ============================================================
# ID REMAPPING CONFIGURATION
# ============================================================

# FK column name → Table whose ID mapping to use
FK_COLUMN_MAP = {
    'companyId':     'Companies',
    'userId':        'Users',
    'contactId':     'Contacts',
    'ticketId':      'Tickets',
    'queueId':       'Queues',
    'whatsappId':    'Whatsapps',
    'tagId':         'Tags',
    'funnelId':      'Funnels',
    'chatId':        'Chats',
    'campaignId':    'Campaigns',
    'contactListId': 'ContactLists',
}

# Tables where 'parentId' is a self-reference
SELF_REF_PARENT = {'QueueOptions'}

# Columns containing media file paths that need companyId/contactId/ticketId remapping
MEDIA_COLUMNS = {
    'Messages':       ['mediaUrl'],
    'Announcements':  ['mediaPath'],
    'Campaigns':      ['mediaPath'],
    'ChatMessages':   ['mediaPath'],
    'QueueOptions':   ['mediaPath'],
    'Queues':         ['mediaPath'],
    'QuickMessages':  ['mediaPath'],
}

# Tables whose IDs need pre-built old→new mappings (referenced as FKs elsewhere)
MAPPED_TABLES = set(COLLECT_IDS_FROM.keys()) | {'Companies'}


# ============================================================
# HELPER FUNCTIONS
# ============================================================

def parse_copy_header(line):
    """Parse a COPY header line and return (table_name, [columns])."""
    match = re.match(r'COPY public\."(\w+)"\s*\((.+?)\)\s*FROM stdin;', line)
    if match:
        table_name = match.group(1)
        columns = [c.strip().strip('"') for c in match.group(2).split(',')]
        return table_name, columns
    return None, None


def col_idx(columns, name):
    """Find column index by name. Returns -1 if not found."""
    try:
        return columns.index(name)
    except ValueError:
        return -1


def fmt(n):
    """Format number with comma separators."""
    return f"{n:,}"


def remap_media_path(value, company_map, contact_map, ticket_map):
    """
    Remap companyId, contactId, ticketId in a media file path.

    Path formats:
      media/{companyId}/{contactId}/{ticketId}/{randomId}/{filename}
      media/{companyId}/{randomId}/{filename}
      http(s)://... → leave unchanged (external URL)

    Returns:
      (new_value, old_path_or_None, new_path_or_None)
    """
    if not value or value == '\\N':
        return value, None, None

    # Don't touch external URLs
    if value.startswith('http://') or value.startswith('https://'):
        return value, None, None

    parts = value.split('/')

    # Must start with 'media/' and have at least 3 segments
    if len(parts) < 3 or parts[0] != 'media':
        return value, None, None

    old_path = value
    changed = False

    # Remap companyId (parts[1])
    if parts[1] in company_map:
        parts[1] = company_map[parts[1]]
        changed = True

    # Remap contactId (parts[2]) — only if it's a known contact ID
    if len(parts) >= 4 and parts[2] in contact_map:
        parts[2] = contact_map[parts[2]]
        changed = True

    # Remap ticketId (parts[3]) — only if it's a known ticket ID
    if len(parts) >= 5 and parts[3] in ticket_map:
        parts[3] = ticket_map[parts[3]]
        changed = True

    if changed:
        new_path = '/'.join(parts)
        return new_path, old_path, new_path

    return value, None, None


# ============================================================
# DATABASE FUNCTIONS (via psql subprocess)
# ============================================================

def query_db(sql, db_host, db_name, db_user, db_port='5432'):
    """Execute a SQL query via psql and return the output string."""
    env = os.environ.copy()
    cmd = [
        'psql', '-h', db_host, '-p', str(db_port), '-U', db_user,
        '-d', db_name, '-t', '-A', '-c', sql
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, env=env)
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def check_db_not_empty(db_params):
    """Verify the target database has tables (is not empty)."""
    result = query_db(
        "SELECT count(*) FROM information_schema.tables "
        "WHERE table_schema = 'public'",
        **db_params
    )
    if result is None:
        print("ERROR: Cannot connect to database.")
        sys.exit(1)
    return int(result) > 0


def get_max_ids(db_params):
    """Get MAX(id) for each data table in the target database."""
    max_ids = {}
    for table in sorted(ALL_DATA_TABLES):
        result = query_db(
            f'SELECT COALESCE(MAX("id"), 0) FROM "{table}"',
            **db_params
        )
        if result is not None:
            try:
                max_ids[table] = int(result)
            except ValueError:
                max_ids[table] = 0
        else:
            # Table might not exist in target — set to 0
            max_ids[table] = 0
    return max_ids


# ============================================================
# PASS 1: Scan dump, identify source company, collect IDs
# ============================================================

def pass1_scan(dump_path):
    """
    Scan the dump to:
    1. Find all company IDs (validate exactly 1 besides company 1)
    2. Collect IDs for tables in COLLECT_IDS_FROM (for FK reference sets)
    3. Also collect Companies IDs

    Returns:
      - source_company_id (str): The single non-admin company ID
      - table_old_ids (dict): {table_name: set_of_old_ids} for MAPPED_TABLES
      - id_sets (dict): {set_name: set_of_ids} for indirect table filtering
    """
    print("=== Pass 1: Scanning dump ===")
    start = time.time()
    line_count = 0

    company_ids_found = set()
    table_old_ids = {}  # table → set of old IDs (for MAPPED_TABLES)
    id_sets = {name: set() for name in set(COLLECT_IDS_FROM.values())}

    with open(dump_path, 'r', encoding='utf-8', errors='replace') as f:
        in_copy = False
        current_table = None
        cid_idx_val = -1
        id_idx_val = -1
        collect_set_name = None

        for line in f:
            line_count += 1
            if line_count % 2_000_000 == 0:
                print(f"  {fmt(line_count)} lines ({time.time()-start:.1f}s)")

            if not in_copy:
                table, cols = parse_copy_header(line.rstrip('\n'))
                if table is not None:
                    in_copy = True
                    current_table = table

                    if table == 'Companies':
                        id_idx_val = col_idx(cols, 'id')
                        cid_idx_val = -1
                        collect_set_name = None
                    elif table in COLLECT_IDS_FROM:
                        cid_idx_val = col_idx(cols, 'companyId')
                        id_idx_val = col_idx(cols, 'id')
                        collect_set_name = COLLECT_IDS_FROM[table]
                    else:
                        cid_idx_val = -1
                        id_idx_val = -1
                        collect_set_name = None
            else:
                stripped = line.rstrip('\n')
                if stripped == '\\.':
                    in_copy = False
                    current_table = None
                    collect_set_name = None
                    continue

                fields = stripped.split('\t')

                if current_table == 'Companies':
                    if id_idx_val >= 0 and id_idx_val < len(fields):
                        cid = fields[id_idx_val]
                        if cid != ADMIN_COMPANY_ID:
                            company_ids_found.add(cid)
                            table_old_ids.setdefault('Companies', set()).add(cid)

                elif collect_set_name is not None:
                    if cid_idx_val >= 0 and cid_idx_val < len(fields):
                        cid = fields[cid_idx_val]
                        if cid != ADMIN_COMPANY_ID:
                            row_id = fields[id_idx_val] if id_idx_val >= 0 and id_idx_val < len(fields) else None
                            if row_id:
                                table_old_ids.setdefault(current_table, set()).add(row_id)
                                id_sets[collect_set_name].add(row_id)

    elapsed = time.time() - start
    print(f"  Done: {fmt(line_count)} lines in {elapsed:.1f}s")
    print(f"  Companies found (besides company 1): {sorted(company_ids_found)}")

    for table, ids in sorted(table_old_ids.items()):
        print(f"    {table}: {fmt(len(ids))} IDs")

    # Validate exactly 1 company
    if len(company_ids_found) == 0:
        print("\nERROR: No companies found in backup (besides company 1).")
        print("The backup must contain exactly one company to import.")
        sys.exit(1)
    if len(company_ids_found) > 1:
        print(f"\nERROR: Found {len(company_ids_found)} companies in backup: {sorted(company_ids_found)}")
        print("The backup must contain exactly ONE company (besides company 1).")
        print("Use ticketz-filter.py to create a single-company backup first.")
        sys.exit(1)

    source_company_id = company_ids_found.pop()
    print(f"\n  Source company ID: {source_company_id}")

    return source_company_id, table_old_ids, id_sets


# ============================================================
# BUILD ID MAPPINGS
# ============================================================

def build_id_maps(table_old_ids, max_ids):
    """
    Build old→new ID mappings for MAPPED_TABLES.

    For each table, sorts the old IDs and assigns new sequential IDs
    starting from MAX(id)+1 in the target database.

    Returns:
      id_maps: {table_name: {old_id_str: new_id_str}}
    """
    id_maps = {}

    for table in sorted(MAPPED_TABLES):
        old_ids = table_old_ids.get(table, set())
        if not old_ids:
            continue

        base = max_ids.get(table, 0)
        sorted_old = sorted(old_ids, key=lambda x: int(x))

        mapping = {}
        for i, old_id in enumerate(sorted_old):
            new_id = str(base + i + 1)
            mapping[old_id] = new_id

        id_maps[table] = mapping
        old_range = f"{sorted_old[0]}-{sorted_old[-1]}" if len(sorted_old) > 1 else sorted_old[0]
        new_start = str(base + 1)
        new_end = str(base + len(sorted_old))
        new_range = f"{new_start}-{new_end}" if len(sorted_old) > 1 else new_start
        print(f"  {table}: {fmt(len(mapping))} IDs ({old_range} → {new_range})")

    return id_maps


# ============================================================
# PASS 2: Rewrite dump with remapped IDs
# ============================================================

def pass2_rewrite(dump_path, output_path, source_company_id, id_maps, id_sets, max_ids):
    """
    Read the dump and generate the import SQL with remapped IDs.

    Skips:
    - Global tables (already exist in target)
    - Company 1 data (target has its own company 1)
    - Tables with no matching rows

    Returns:
      tables_imported: [(table_name, row_count), ...]
      media_ops: [(old_media_path, new_media_path), ...]
    """
    print("\n=== Pass 2: Generating import SQL ===")
    start = time.time()
    line_count = 0

    # ID counters for tables NOT in MAPPED_TABLES (assign sequential new IDs)
    id_counters = {}
    for table in ALL_DATA_TABLES:
        id_counters[table] = max_ids.get(table, 0)

    # Convenience references
    company_map = id_maps.get('Companies', {})
    contact_map = id_maps.get('Contacts', {})
    ticket_map = id_maps.get('Tickets', {})

    tables_imported = []
    media_ops = []
    warnings = []

    with open(dump_path, 'r', encoding='utf-8', errors='replace') as fin, \
         open(output_path, 'w', encoding='utf-8') as fout:

        # Write transaction header
        new_company_id = company_map.get(source_company_id, '?')
        fout.write(f"-- Ticketz Import\n")
        fout.write(f"-- Source company: {source_company_id} -> New company: {new_company_id}\n")
        fout.write(f"-- Generated: {time.strftime('%Y-%m-%d %H:%M:%S')}\n\n")
        fout.write("BEGIN;\n\n")
        fout.write("-- Disable FK trigger checks during bulk import\n")
        fout.write("SET session_replication_role = replica;\n\n")

        # State tracking
        in_copy = False
        current_table = None
        columns = None
        copy_header_line = None
        row_buffer = []

        # Column indices for current table
        id_col = -1
        remap_cols = {}       # col_index → mapping_table_name
        media_col_idxs = []   # column indices with media paths
        is_data_table = False
        is_company_table = False
        is_self_ref_table = False

        # For determining row membership
        membership_col_idx = -1
        membership_mode = None  # 'companyId', 'fk_set', 'company_table'
        membership_set = None

        for line in fin:
            line_count += 1
            if line_count % 2_000_000 == 0:
                print(f"  {fmt(line_count)} lines ({time.time()-start:.1f}s)")

            if not in_copy:
                table, cols = parse_copy_header(line.rstrip('\n'))
                if table is not None:
                    in_copy = True
                    current_table = table
                    columns = cols
                    copy_header_line = line
                    row_buffer = []

                    # Determine if this table should be imported
                    if table in GLOBAL_TABLES:
                        is_data_table = False
                    elif table in ALL_DATA_TABLES:
                        is_data_table = True
                    else:
                        is_data_table = False

                    is_company_table = (table == 'Companies')
                    is_self_ref_table = (table in SELF_REF_PARENT)

                    if is_data_table:
                        # Find ID column
                        id_col = col_idx(cols, 'id')

                        # Build FK remapping index
                        remap_cols = {}
                        for ci, col_name in enumerate(cols):
                            if col_name == 'id':
                                continue
                            if col_name in FK_COLUMN_MAP:
                                remap_cols[ci] = FK_COLUMN_MAP[col_name]
                            elif col_name == 'parentId' and is_self_ref_table:
                                remap_cols[ci] = table  # self-reference marker

                        # Media column indices
                        media_col_idxs = []
                        if table in MEDIA_COLUMNS:
                            for mc in MEDIA_COLUMNS[table]:
                                idx = col_idx(cols, mc)
                                if idx >= 0:
                                    media_col_idxs.append(idx)

                        # Determine membership filter
                        if is_company_table:
                            membership_mode = 'company_table'
                            membership_col_idx = col_idx(cols, 'id')
                            membership_set = None
                        elif table in TABLES_WITH_COMPANY_ID:
                            membership_mode = 'companyId'
                            membership_col_idx = col_idx(cols, 'companyId')
                            membership_set = None
                        elif table in INDIRECT_TABLES:
                            fk_col, set_name = INDIRECT_TABLES[table]
                            membership_mode = 'fk_set'
                            membership_col_idx = col_idx(cols, fk_col)
                            membership_set = id_sets.get(set_name, set())
                        else:
                            membership_mode = None
                continue

            # We're inside a COPY block
            stripped = line.rstrip('\n')

            if stripped == '\\.':
                # End of COPY block
                in_copy = False

                if is_data_table and row_buffer:
                    # Special post-processing for self-referencing tables
                    if is_self_ref_table:
                        row_buffer = _postprocess_self_ref(
                            current_table, row_buffer, columns,
                            id_col, remap_cols, id_counters
                        )

                    # Write the COPY block
                    fout.write(f"-- {current_table}: {len(row_buffer)} rows\n")
                    fout.write(copy_header_line)
                    for row_fields in row_buffer:
                        fout.write('\t'.join(row_fields) + '\n')
                    fout.write('\\.\n\n')
                    tables_imported.append((current_table, len(row_buffer)))

                current_table = None
                continue

            if not is_data_table:
                continue

            fields = stripped.split('\t')

            # --- Check membership (using ORIGINAL values) ---
            include = False
            if membership_mode == 'company_table':
                if membership_col_idx >= 0 and membership_col_idx < len(fields):
                    include = (fields[membership_col_idx] == source_company_id)
            elif membership_mode == 'companyId':
                if membership_col_idx >= 0 and membership_col_idx < len(fields):
                    cid = fields[membership_col_idx]
                    include = (cid != ADMIN_COMPANY_ID and cid == source_company_id)
            elif membership_mode == 'fk_set':
                if membership_col_idx >= 0 and membership_col_idx < len(fields):
                    fk_val = fields[membership_col_idx]
                    include = (fk_val in membership_set)

            if not include:
                continue

            # --- Remap FK columns (except self-reference, handled in post-processing) ---
            for ci, mapping_table in remap_cols.items():
                if ci >= len(fields):
                    continue
                if mapping_table == current_table:
                    continue  # self-reference — handled in post-processing
                old_val = fields[ci]
                if old_val == '\\N':
                    continue
                if mapping_table in id_maps:
                    if old_val in id_maps[mapping_table]:
                        fields[ci] = id_maps[mapping_table][old_val]
                    else:
                        msg = (f"{current_table}.{columns[ci]}={old_val} "
                               f"not in {mapping_table} mapping → NULL")
                        warnings.append(msg)
                        fields[ci] = '\\N'

            # --- Remap ID column ---
            if id_col >= 0 and id_col < len(fields):
                old_id = fields[id_col]
                if current_table in id_maps:
                    if old_id in id_maps[current_table]:
                        fields[id_col] = id_maps[current_table][old_id]
                    else:
                        id_counters[current_table] += 1
                        fields[id_col] = str(id_counters[current_table])
                elif not is_self_ref_table:
                    # Assign sequential new ID
                    id_counters[current_table] += 1
                    fields[id_col] = str(id_counters[current_table])
                # Self-ref tables: ID assigned in post-processing

            # --- Remap media paths ---
            for mi in media_col_idxs:
                if mi < len(fields):
                    old_val = fields[mi]
                    new_val, old_path, new_path = remap_media_path(
                        old_val, company_map, contact_map, ticket_map
                    )
                    fields[mi] = new_val
                    if old_path and new_path:
                        media_ops.append((old_path, new_path))

            row_buffer.append(fields)

        # --- Write sequence updates and close transaction ---
        fout.write("\n-- Re-enable FK trigger checks\n")
        fout.write("SET session_replication_role = DEFAULT;\n\n")

        fout.write("-- Update sequences to reflect imported data\n")
        all_imported = set(t for t, _ in tables_imported)
        for table in sorted(all_imported):
            fout.write(
                f'SELECT setval(\'"{table}_id_seq"\', '
                f'(SELECT COALESCE(MAX("id"), 1) FROM "{table}"), true);\n'
            )

        fout.write("\nCOMMIT;\n")

    elapsed = time.time() - start
    print(f"  Done: {fmt(line_count)} lines in {elapsed:.1f}s")

    # Print import summary
    total_rows = sum(n for _, n in tables_imported)
    print(f"\n  Tables: {len(tables_imported)}, Total rows: {fmt(total_rows)}")
    for table, count in tables_imported:
        print(f"    {table}: {fmt(count)}")

    if warnings:
        print(f"\n  Warnings ({len(warnings)}):")
        # Show first 20 warnings
        for w in warnings[:20]:
            print(f"    ⚠ {w}")
        if len(warnings) > 20:
            print(f"    ... and {len(warnings) - 20} more")

    if media_ops:
        print(f"\n  Media path remappings: {fmt(len(media_ops))}")

    return tables_imported, media_ops


def _postprocess_self_ref(table, rows, columns, id_col, remap_cols, id_counters):
    """
    Post-process a self-referencing table (e.g., QueueOptions).

    1. Assign new sequential IDs
    2. Build old→new ID mapping
    3. Remap parentId using the mapping
    """
    if id_col < 0:
        return rows

    # Find parentId column
    parent_col = -1
    for ci, mapping_table in remap_cols.items():
        if mapping_table == table:
            parent_col = ci
            break

    # Assign new IDs and build mapping
    self_map = {}
    base = id_counters[table]
    for i, fields in enumerate(rows):
        old_id = fields[id_col]
        new_id = str(base + i + 1)
        self_map[old_id] = new_id
        fields[id_col] = new_id
    id_counters[table] = base + len(rows)

    # Remap parentId
    if parent_col >= 0:
        for fields in rows:
            if parent_col < len(fields):
                old_val = fields[parent_col]
                if old_val != '\\N' and old_val in self_map:
                    fields[parent_col] = self_map[old_val]

    return rows


# ============================================================
# MEDIA FILE OPERATIONS
# ============================================================

def move_media_files(media_src, media_dst, source_company_id, new_company_id,
                     contact_map, ticket_map):
    """
    Copy media files from extracted backup to target volume,
    remapping directory names (companyId, contactId, ticketId).

    Source structure: {media_src}/media/{oldCompanyId}/{contactId}/{ticketId}/{randomId}/{file}
    Target structure: {media_dst}/media/{newCompanyId}/{newContactId}/{newTicketId}/{randomId}/{file}
    """
    src_media = os.path.join(media_src, 'media', source_company_id)
    if not os.path.isdir(src_media):
        print(f"  No media directory found at: {src_media}")
        return 0

    dst_base = os.path.join(media_dst, 'media', new_company_id)
    files_copied = 0

    print(f"  Source: {src_media}")
    print(f"  Target: {dst_base}")

    for root, dirs, files in os.walk(src_media):
        for filename in files:
            src_file = os.path.join(root, filename)
            # Get relative path from the company directory
            rel_path = os.path.relpath(src_file, src_media)
            parts = rel_path.replace('\\', '/').split('/')

            # Remap contactId and ticketId in the path
            if len(parts) >= 3:
                # parts[0] might be contactId, parts[1] might be ticketId
                if parts[0] in contact_map:
                    parts[0] = contact_map[parts[0]]
                if len(parts) >= 2 and parts[1] in ticket_map:
                    parts[1] = ticket_map[parts[1]]

            dst_rel = os.path.join(*parts) if parts else filename
            dst_file = os.path.join(dst_base, dst_rel)
            dst_dir = os.path.dirname(dst_file)

            os.makedirs(dst_dir, exist_ok=True)

            # Copy file
            with open(src_file, 'rb') as sf, open(dst_file, 'wb') as df:
                while True:
                    chunk = sf.read(1024 * 1024)  # 1MB chunks
                    if not chunk:
                        break
                    df.write(chunk)

            files_copied += 1

    return files_copied


# ============================================================
# MAIN
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description='Import a single company from a Ticketz backup into an existing database.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Dry run (generate SQL without executing):
  python3 ticketz-import.py dump.sql --db-host postgres --db-name ticketz --db-user ticketz --dry-run --output preview.sql

  # Import with media:
  python3 ticketz-import.py dump.sql --db-host postgres --db-name ticketz --db-user ticketz --output import.sql --media-src /tmp/backup/backend-public --media-dst /backend-public

Known limitations:
  - Settings values containing queue/user IDs (as text) are NOT remapped automatically
  - JSON columns with embedded IDs are NOT remapped
  - Plans table IDs must match between source and target installations
  - S3/external media URLs are left unchanged (use a different bucket for the target)
  - Baileys/BaileysKeys session data may not work on the new server
  - profilePicUrl in Contacts (WhatsApp profile picture URL) is not remapped (external URL)
        """
    )

    parser.add_argument('dump', help='Path to the db_dump.sql file')
    parser.add_argument('--db-host', default=os.environ.get('DB_HOST', 'postgres'),
                        help='Database host (default: DB_HOST env or "postgres")')
    parser.add_argument('--db-name', default=os.environ.get('DB_NAME', 'ticketz'),
                        help='Database name (default: DB_NAME env or "ticketz")')
    parser.add_argument('--db-user', default=os.environ.get('DB_USER', 'ticketz'),
                        help='Database user (default: DB_USER env or "ticketz")')
    parser.add_argument('--db-port', default=os.environ.get('DB_PORT', '5432'),
                        help='Database port (default: DB_PORT env or "5432")')
    parser.add_argument('--output', default=None,
                        help='Output file for import SQL (default: <dump>.import.sql)')
    parser.add_argument('--dry-run', action='store_true',
                        help='Generate import SQL without executing or moving files')
    parser.add_argument('--media-src', default=None,
                        help='Source media directory (extracted from backup)')
    parser.add_argument('--media-dst', default=None,
                        help='Destination media directory (live volume mount)')
    parser.add_argument('--media-map', default=None,
                        help='Output JSON file with ID mappings (for debugging)')

    args = parser.parse_args()

    dump_path = args.dump
    output_path = args.output or (dump_path + '.import.sql')
    db_params = {
        'db_host': args.db_host,
        'db_name': args.db_name,
        'db_user': args.db_user,
        'db_port': args.db_port,
    }

    # Validate dump file exists
    if not os.path.exists(dump_path):
        print(f"ERROR: File not found: {dump_path}")
        sys.exit(1)

    print(f"Ticketz Import")
    print(f"{'=' * 60}")
    print(f"Dump file: {dump_path} ({os.path.getsize(dump_path) / (1024*1024):.1f} MB)")
    print(f"Database:  {args.db_host}:{args.db_port}/{args.db_name}")
    print(f"Dry run:   {'Yes' if args.dry_run else 'No'}")
    print()

    # ---- Pass 1: Scan dump ----
    source_company_id, table_old_ids, id_sets = pass1_scan(dump_path)

    # ---- Query target database ----
    print("\n=== Querying target database ===")

    if not check_db_not_empty(db_params):
        print("\nERROR: Target database is empty (no tables).")
        print("Use 'sidekick2 restore' to restore the backup instead of import.")
        sys.exit(1)

    print("  Database has tables — OK")

    max_ids = get_max_ids(db_params)
    non_zero = {t: v for t, v in max_ids.items() if v > 0}
    print(f"  Tables with data: {len(non_zero)}")
    for t, v in sorted(non_zero.items()):
        print(f"    {t}: max(id) = {fmt(v)}")

    # ---- Build ID mappings ----
    print("\n=== Building ID mappings ===")
    id_maps = build_id_maps(table_old_ids, max_ids)

    if 'Companies' not in id_maps:
        print("\nERROR: No company data to import.")
        sys.exit(1)

    new_company_id = id_maps['Companies'].get(source_company_id, '?')
    print(f"\n  Company {source_company_id} → {new_company_id}")

    # ---- Pass 2: Generate import SQL ----
    tables_imported, media_ops = pass2_rewrite(
        dump_path, output_path, source_company_id,
        id_maps, id_sets, max_ids
    )

    output_size = os.path.getsize(output_path)
    print(f"\n  Import SQL: {output_path} ({output_size / (1024*1024):.1f} MB)")

    # ---- Output media map ----
    if args.media_map:
        media_map_data = {
            'source_company_id': source_company_id,
            'new_company_id': new_company_id,
            'company_map': id_maps.get('Companies', {}),
            'contact_map': id_maps.get('Contacts', {}),
            'ticket_map': id_maps.get('Tickets', {}),
            'media_path_remaps': len(media_ops),
        }
        with open(args.media_map, 'w') as f:
            json.dump(media_map_data, f, indent=2)
        print(f"  Media map: {args.media_map}")

    # ---- Move media files (if not dry-run) ----
    if args.media_src and args.media_dst and not args.dry_run:
        print(f"\n=== Moving media files ===")
        contact_map = id_maps.get('Contacts', {})
        ticket_map = id_maps.get('Tickets', {})
        files_copied = move_media_files(
            args.media_src, args.media_dst,
            source_company_id, new_company_id,
            contact_map, ticket_map
        )
        print(f"  Files copied: {fmt(files_copied)}")
    elif args.media_src and args.dry_run:
        print(f"\n  DRY RUN: Media files would be copied from {args.media_src}")

    # ---- Summary ----
    print(f"\n{'=' * 60}")
    if args.dry_run:
        print(f"DRY RUN complete.")
        print(f"Import SQL generated at: {output_path}")
        print(f"Review the SQL file before executing.")
        print(f"\nTo execute manually:")
        print(f"  psql -h {args.db_host} -p {args.db_port} -U {args.db_user} "
              f"-d {args.db_name} --single-transaction < {output_path}")
    else:
        print(f"Import SQL generated at: {output_path}")
        print(f"Ready for execution by sidekick2.sh")

    print()
    return 0


if __name__ == '__main__':
    sys.exit(main())
