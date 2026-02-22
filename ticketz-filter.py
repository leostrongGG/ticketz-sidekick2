#!/usr/bin/env python3
"""
Ticketz SQL Dump Filter
=======================
Filtra um db_dump.sql do Ticketz para manter apenas empresas selecionadas.

Projetado para uso dentro do container sidekick2, integrado ao sidekick.sh.
Pode também ser usado standalone.

A empresa 1 (admin/sistema) é SEMPRE incluída automaticamente.
Suas conexões WhatsApp são removidas para evitar conflitos.

Uso:
  python3 ticketz-filter.py <db_dump.sql> <companyIds> [--media-list <arquivo>]

  --media-list <arquivo>  Grava lista de arquivos de mídia referenciados
                          (para o sidekick.sh filtrar as pastas de dados)

Saída:
  - O db_dump.sql é filtrado in-place (substitui o original)
  - Se --media-list informado, grava um arquivo com os nomes de mídia a manter
"""

import sys
import re
import os
import time


# ============================================================
# CLASSIFICAÇÃO DAS TABELAS
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

MEDIA_COLUMNS = {
    'Messages':       ['mediaUrl'],
    'Announcements':  ['mediaPath'],
    'Campaigns':      ['mediaPath'],
    'ChatMessages':   ['mediaPath'],
    'QueueOptions':   ['mediaPath'],
    'Queues':         ['mediaPath'],
    'QuickMessages':  ['mediaPath'],
}

ADMIN_COMPANY_ID = '1'
COMPANY1_EXCLUDE_TABLES = {'Whatsapps'}


# ============================================================
# FUNÇÕES AUXILIARES
# ============================================================

def parse_company_ids(spec):
    """Analisa IDs: "263", "10,15,22", "10-20", "1-50,263"."""
    ids = set()
    for part in spec.split(','):
        part = part.strip()
        if '-' in part:
            start, end = part.split('-', 1)
            for i in range(int(start), int(end) + 1):
                ids.add(str(i))
        else:
            ids.add(part)
    ids.add(ADMIN_COMPANY_ID)
    return ids


def parse_copy_header(line):
    match = re.match(r'COPY public\."(\w+)"\s*\((.+?)\)\s*FROM stdin;', line)
    if match:
        table_name = match.group(1)
        columns = [c.strip().strip('"') for c in match.group(2).split(',')]
        return table_name, columns
    return None, None


def col_idx(columns, name):
    try:
        return columns.index(name)
    except ValueError:
        return -1


def fmt(n):
    return f"{n:,}"


# ============================================================
# PASSAGEM 1: COLETA DE IDs
# ============================================================

def pass1_collect_ids(dump_path, company_ids):
    id_sets = {name: set() for name in set(COLLECT_IDS_FROM.values())}
    company1_whatsapp_ids = set()

    print("=== Pass 1: Collecting IDs ===")
    start = time.time()
    line_count = 0

    with open(dump_path, 'r', encoding='utf-8', errors='replace') as f:
        in_copy = False
        current_table = None
        cid_idx = -1
        id_i = -1
        collect_set = None

        for line in f:
            line_count += 1
            if line_count % 2_000_000 == 0:
                print(f"  {fmt(line_count)} lines ({time.time()-start:.1f}s)")

            if not in_copy:
                table, cols = parse_copy_header(line.rstrip('\n'))
                if table is not None:
                    in_copy = True
                    current_table = table
                    if table in COLLECT_IDS_FROM:
                        cid_idx = col_idx(cols, 'companyId')
                        id_i = col_idx(cols, 'id')
                        collect_set = COLLECT_IDS_FROM[table]
                        if cid_idx < 0 or id_i < 0:
                            collect_set = None
                    else:
                        collect_set = None
            else:
                stripped = line.rstrip('\n')
                if stripped == '\\.':
                    in_copy = False
                    current_table = None
                    collect_set = None
                elif collect_set:
                    fields = stripped.split('\t')
                    if cid_idx < len(fields) and id_i < len(fields):
                        cid = fields[cid_idx]
                        rid = fields[id_i]
                        if cid in company_ids:
                            if cid == ADMIN_COMPANY_ID and current_table in COMPANY1_EXCLUDE_TABLES:
                                company1_whatsapp_ids.add(rid)
                            else:
                                id_sets[collect_set].add(rid)

    elapsed = time.time() - start
    print(f"  Done: {fmt(line_count)} lines in {elapsed:.1f}s")
    for name, ids in sorted(id_sets.items()):
        if ids:
            print(f"    {name}: {fmt(len(ids))}")
    if company1_whatsapp_ids:
        print(f"    (company 1: {fmt(len(company1_whatsapp_ids))} WhatsApp connections excluded)")

    return id_sets, company1_whatsapp_ids


# ============================================================
# PASSAGEM 2: ESCRITA FILTRADA
# ============================================================

def pass2_write_filtered(dump_path, output_path, company_ids, id_sets, c1_wa_ids):
    print("\n=== Pass 2: Writing filtered dump ===")
    start = time.time()
    line_count = 0
    total_kept = 0
    total_skipped = 0
    media_files = set()

    with open(dump_path, 'r', encoding='utf-8', errors='replace') as fin, \
         open(output_path, 'w', encoding='utf-8') as fout:

        in_copy = False
        current_table = None
        filter_mode = None
        filter_idx = -1
        filter_set = None
        media_indices = []

        for line in fin:
            line_count += 1
            if line_count % 2_000_000 == 0:
                print(f"  {fmt(line_count)} lines ({time.time()-start:.1f}s)")

            if not in_copy:
                table, cols = parse_copy_header(line.rstrip('\n'))
                if table is not None:
                    in_copy = True
                    current_table = table

                    if table == 'Companies':
                        filter_mode = 'company_table'
                        filter_idx = col_idx(cols, 'id')
                    elif table in TABLES_WITH_COMPANY_ID:
                        filter_mode = 'company_id'
                        filter_idx = col_idx(cols, 'companyId')
                    elif table in INDIRECT_TABLES:
                        fk_col, set_name = INDIRECT_TABLES[table]
                        filter_idx = col_idx(cols, fk_col)
                        if filter_idx >= 0:
                            filter_mode = 'indirect'
                            filter_set = id_sets.get(set_name, set())
                        else:
                            filter_mode = 'global'
                    else:
                        filter_mode = 'global'

                    media_indices = []
                    if table in MEDIA_COLUMNS:
                        for mc in MEDIA_COLUMNS[table]:
                            idx = col_idx(cols, mc)
                            if idx >= 0:
                                media_indices.append(idx)

                    fout.write(line)
                else:
                    fout.write(line)
            else:
                stripped = line.rstrip('\n')
                if stripped == '\\.':
                    fout.write(line)
                    in_copy = False
                    current_table = None
                    filter_mode = None
                    media_indices = []

                elif filter_mode == 'global':
                    fout.write(line)
                    total_kept += 1

                elif filter_mode in ('company_id', 'company_table', 'indirect'):
                    fields = stripped.split('\t')
                    keep = False

                    if filter_mode == 'company_id':
                        if filter_idx < len(fields):
                            cid = fields[filter_idx]
                            if cid in company_ids:
                                if cid == ADMIN_COMPANY_ID and current_table in COMPANY1_EXCLUDE_TABLES:
                                    keep = False
                                else:
                                    keep = True
                    elif filter_mode == 'company_table':
                        keep = (filter_idx < len(fields) and
                                fields[filter_idx] in company_ids)
                    elif filter_mode == 'indirect':
                        keep = (filter_idx < len(fields) and
                                fields[filter_idx] in filter_set)

                    if keep:
                        fout.write(line)
                        total_kept += 1
                        for mi in media_indices:
                            if mi < len(fields):
                                val = fields[mi]
                                if val and val != '\\N' and val.strip():
                                    fn = val.replace('\\', '/').split('/')[-1]
                                    if fn:
                                        media_files.add(fn)
                    else:
                        total_skipped += 1
                else:
                    fout.write(line)

    elapsed = time.time() - start
    print(f"  Done: {fmt(line_count)} lines in {elapsed:.1f}s")
    print(f"  Kept: {fmt(total_kept)}, Skipped: {fmt(total_skipped)}")
    print(f"  Media references: {fmt(len(media_files))}")

    return total_kept, total_skipped, media_files


# ============================================================
# PRINCIPAL
# ============================================================

def main():
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)

    dump_path = sys.argv[1]
    company_spec = sys.argv[2]

    # Parse --media-list option
    media_list_file = None
    for i, arg in enumerate(sys.argv):
        if arg == '--media-list' and i + 1 < len(sys.argv):
            media_list_file = sys.argv[i + 1]

    if not os.path.exists(dump_path):
        print(f"ERROR: File not found: {dump_path}")
        sys.exit(1)

    company_ids = parse_company_ids(company_spec)
    user_ids = sorted(int(x) for x in company_ids)
    print(f"Filtering for companies: {', '.join(str(x) for x in user_ids)}")
    print(f"  (company 1/admin always included)")
    print()

    original_size = os.path.getsize(dump_path)
    print(f"Input: {dump_path} ({original_size / (1024*1024):.1f} MB)")
    print()

    # Pass 1: Collect IDs
    id_sets, c1_wa_ids = pass1_collect_ids(dump_path, company_ids)

    # Pass 2: Write filtered dump
    filtered_path = dump_path + '.filtered'
    total_kept, total_skipped, media_files = pass2_write_filtered(
        dump_path, filtered_path, company_ids, id_sets, c1_wa_ids
    )

    # Replace original with filtered
    os.remove(dump_path)
    os.rename(filtered_path, dump_path)

    filtered_size = os.path.getsize(dump_path)
    print(f"\nDump: {original_size/(1024*1024):.1f} MB -> {filtered_size/(1024*1024):.1f} MB")
    reduction = (1 - filtered_size / original_size) * 100 if original_size > 0 else 0
    print(f"Reduction: {reduction:.1f}%")

    # Write media list if requested
    if media_list_file:
        with open(media_list_file, 'w') as f:
            for fn in sorted(media_files):
                f.write(fn + '\n')
        print(f"\nMedia list written to: {media_list_file} ({len(media_files)} files)")

    print("\nFilter complete.")


if __name__ == '__main__':
    main()
