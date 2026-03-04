#!/usr/bin/env python3
"""
Ticketz Schema Verifier
=======================
Verifies and repairs missing FK constraints after restore or import.

Parses the dump SQL file to extract expected FK constraints, then
compares them against actual FK constraints in the database.
Missing FKs are automatically repaired by:
  1. Fixing orphan data (SET NULL or DELETE based on FK action)
  2. Creating the missing FK constraint

This prevents the issue where psql silently skips FK creation
when orphan data prevents constraint validation during restore.

Usage (called by sidekick2.sh):
  python3 ticketz-verify.py <db_dump.sql> \\
    --db-host HOST --db-name NAME --db-user USER [--db-port PORT]
"""

import sys
import re
import os
import subprocess


# ============================================================
# DUMP PARSING
# ============================================================

def parse_fks_from_dump(dump_path):
    """
    Extract FK constraint definitions from a pg_dump SQL file.

    pg_dump format (2-line):
      ALTER TABLE ONLY public."TableName"
          ADD CONSTRAINT "name" FOREIGN KEY ("col") REFERENCES public."Ref"(id) ON UPDATE ... ON DELETE ...;

    Returns list of dicts with FK metadata.
    """
    fks = []
    buffer = ''

    with open(dump_path, 'r', encoding='utf-8', errors='replace') as f:
        for line in f:
            stripped = line.strip()

            # Start of ALTER TABLE (without ADD CONSTRAINT on same line)
            if stripped.startswith('ALTER TABLE') and 'ADD CONSTRAINT' not in stripped:
                buffer = stripped
                continue

            # Continuation of ALTER TABLE
            if buffer:
                buffer += ' ' + stripped
                if buffer.endswith(';'):
                    if 'FOREIGN KEY' in buffer:
                        fk = _parse_fk_statement(buffer)
                        if fk:
                            fks.append(fk)
                    buffer = ''
                continue

            # Single-line ALTER TABLE ADD CONSTRAINT
            if ('ALTER TABLE' in stripped and 'ADD CONSTRAINT' in stripped
                    and 'FOREIGN KEY' in stripped and stripped.endswith(';')):
                fk = _parse_fk_statement(stripped)
                if fk:
                    fks.append(fk)

    return fks


def _parse_fk_statement(stmt):
    """Parse a single ALTER TABLE ADD CONSTRAINT FOREIGN KEY statement."""

    # Extract table name
    table_m = re.search(r'ALTER TABLE (?:ONLY )?public\."(\w+)"', stmt)
    if not table_m:
        return None

    # Extract constraint name
    name_m = re.search(r'ADD CONSTRAINT "(\w+)"', stmt)
    if not name_m:
        return None

    # Extract FK columns (may be multi-column)
    fk_m = re.search(r'FOREIGN KEY\s*\((.+?)\)', stmt)
    if not fk_m:
        return None

    # Extract referenced table and columns
    ref_m = re.search(r'REFERENCES\s+public\."(\w+)"\((.+?)\)', stmt)
    if not ref_m:
        return None

    # Extract ON UPDATE / ON DELETE actions
    on_update = 'NO ACTION'
    on_delete = 'NO ACTION'
    up_m = re.search(r'ON UPDATE (CASCADE|SET NULL|SET DEFAULT|RESTRICT|NO ACTION)', stmt, re.I)
    del_m = re.search(r'ON DELETE (CASCADE|SET NULL|SET DEFAULT|RESTRICT|NO ACTION)', stmt, re.I)
    if up_m:
        on_update = up_m.group(1).upper()
    if del_m:
        on_delete = del_m.group(1).upper()

    columns = [c.strip().strip('"') for c in fk_m.group(1).split(',')]
    ref_columns = [c.strip().strip('"') for c in ref_m.group(2).split(',')]

    return {
        'table': table_m.group(1),
        'constraint': name_m.group(1),
        'columns': columns,
        'ref_table': ref_m.group(1),
        'ref_columns': ref_columns,
        'on_update': on_update,
        'on_delete': on_delete,
    }


# ============================================================
# DATABASE FUNCTIONS
# ============================================================

def query_db(sql, db_host, db_name, db_user, db_port='5432'):
    """Execute a SQL query via psql and return trimmed output."""
    cmd = [
        'psql', '-h', db_host, '-p', str(db_port), '-U', db_user,
        '-d', db_name, '-t', '-A', '-c', sql
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, env=os.environ.copy())
    if result.returncode != 0:
        print(f"  SQL error: {result.stderr.strip()}")
        return None
    return result.stdout.strip()


def exec_sql(sql, db_host, db_name, db_user, db_port='5432'):
    """Execute a SQL statement via psql. Returns (ok, stdout, stderr)."""
    cmd = [
        'psql', '-h', db_host, '-p', str(db_port), '-U', db_user,
        '-d', db_name, '-c', sql
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, env=os.environ.copy())
    return result.returncode == 0, result.stdout.strip(), result.stderr.strip()


def get_existing_fk_names(db_params):
    """Get set of existing FK constraint names from the database."""
    sql = """
    SELECT conname
    FROM pg_constraint
    WHERE contype = 'f'
      AND connamespace = (SELECT oid FROM pg_namespace WHERE nspname = 'public')
    """
    result = query_db(sql, **db_params)
    if result:
        return set(line.strip() for line in result.split('\n') if line.strip())
    return set()


# ============================================================
# ORPHAN FIX + FK CREATION
# ============================================================

def fix_and_create_fk(fk, db_params):
    """
    Fix orphan data and create a missing FK constraint.

    1. Count orphan rows (referencing non-existent parent)
    2. Fix orphans based on ON DELETE action:
       - SET NULL → UPDATE ... SET col = NULL
       - CASCADE  → DELETE orphan rows
    3. CREATE the FK constraint

    Returns True if FK was successfully created.
    """
    table = fk['table']
    constraint = fk['constraint']
    columns = fk['columns']
    ref_table = fk['ref_table']
    ref_columns = fk['ref_columns']
    on_delete = fk['on_delete']
    on_update = fk['on_update']

    print(f"\n  [{constraint}]")
    print(f"    {table}.{','.join(columns)} -> {ref_table}.{','.join(ref_columns)}")
    print(f"    ON UPDATE {on_update} ON DELETE {on_delete}")

    # Build JOIN condition for orphan detection (supports multi-column FKs)
    join_conds = ' AND '.join(
        f't."{c}" = r."{rc}"' for c, rc in zip(columns, ref_columns)
    )
    not_null_conds = ' AND '.join(f't."{c}" IS NOT NULL' for c in columns)
    null_check = f'r."{ref_columns[0]}" IS NULL'

    # Count orphans
    orphan_sql = (
        f'SELECT count(*) FROM "{table}" t '
        f'LEFT JOIN "{ref_table}" r ON {join_conds} '
        f'WHERE {not_null_conds} AND {null_check}'
    )
    orphan_count_str = query_db(orphan_sql, **db_params)
    orphan_count = int(orphan_count_str) if orphan_count_str else -1

    if orphan_count < 0:
        print(f"    ERROR: Could not count orphans")
        return False

    if orphan_count > 0:
        print(f"    Orphan rows: {orphan_count:,}")

        # Build EXISTS subquery for fix
        exists_conds = ' AND '.join(
            f'r."{rc}" = "{table}"."{c}"' for c, rc in zip(columns, ref_columns)
        )

        if on_delete in ('SET NULL', 'SET DEFAULT'):
            # SET columns to NULL
            set_clause = ', '.join(f'"{c}" = NULL' for c in columns)
            fix_sql = (
                f'UPDATE "{table}" SET {set_clause} '
                f'WHERE {not_null_conds.replace("t.", "")} '
                f'AND NOT EXISTS (SELECT 1 FROM "{ref_table}" r WHERE {exists_conds})'
            )
            ok, out, err = exec_sql(fix_sql, **db_params)
            if ok:
                print(f"    Fixed: SET NULL for {orphan_count:,} rows")
            else:
                print(f"    ERROR fixing orphans: {err}")
                return False

        elif on_delete == 'CASCADE':
            fix_sql = (
                f'DELETE FROM "{table}" '
                f'WHERE {not_null_conds.replace("t.", "")} '
                f'AND NOT EXISTS (SELECT 1 FROM "{ref_table}" r WHERE {exists_conds})'
            )
            ok, out, err = exec_sql(fix_sql, **db_params)
            if ok:
                print(f"    Fixed: DELETED {orphan_count:,} orphan rows")
            else:
                print(f"    ERROR fixing orphans: {err}")
                return False

        else:
            print(f"    WARNING: ON DELETE {on_delete} — cannot auto-fix, skipping")
            return False
    else:
        print(f"    No orphan rows")

    # Build CREATE CONSTRAINT statement
    fk_cols = ', '.join(f'"{c}"' for c in columns)
    ref_cols = ', '.join(f'"{rc}"' for rc in ref_columns)
    actions = ''
    if on_update != 'NO ACTION':
        actions += f' ON UPDATE {on_update}'
    if on_delete != 'NO ACTION':
        actions += f' ON DELETE {on_delete}'

    create_sql = (
        f'ALTER TABLE "{table}" ADD CONSTRAINT "{constraint}" '
        f'FOREIGN KEY ({fk_cols}) REFERENCES "{ref_table}"({ref_cols}){actions}'
    )
    ok, out, err = exec_sql(create_sql, **db_params)
    if ok:
        print(f"    Created constraint OK")
        return True
    else:
        print(f"    FAILED to create constraint: {err}")
        return False


# ============================================================
# MAIN
# ============================================================

def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    dump_path = sys.argv[1]

    db_params = {
        'db_host': 'postgres',
        'db_name': 'ticketz',
        'db_user': 'ticketz',
        'db_port': '5432',
    }

    i = 2
    while i < len(sys.argv):
        arg = sys.argv[i]
        if arg == '--db-host' and i + 1 < len(sys.argv):
            db_params['db_host'] = sys.argv[i + 1]; i += 2
        elif arg == '--db-name' and i + 1 < len(sys.argv):
            db_params['db_name'] = sys.argv[i + 1]; i += 2
        elif arg == '--db-user' and i + 1 < len(sys.argv):
            db_params['db_user'] = sys.argv[i + 1]; i += 2
        elif arg == '--db-port' and i + 1 < len(sys.argv):
            db_params['db_port'] = sys.argv[i + 1]; i += 2
        else:
            i += 1

    if not os.path.exists(dump_path):
        print(f"ERROR: Dump file not found: {dump_path}")
        sys.exit(1)

    print("=== Schema Verification ===")
    print(f"Dump: {dump_path}")
    print()

    # Step 1: Parse expected FKs from dump
    print("Parsing expected FK constraints from dump...")
    expected_fks = parse_fks_from_dump(dump_path)
    print(f"  Found {len(expected_fks)} FK constraints in dump")

    if not expected_fks:
        print("\nWARNING: No FK constraints found in dump. Nothing to verify.")
        sys.exit(0)

    # Step 2: Get existing FKs from database
    print("\nQuerying existing FK constraints from database...")
    existing_names = get_existing_fk_names(db_params)
    print(f"  Found {len(existing_names)} FK constraints in database")

    # Step 3: Find missing FKs
    missing_fks = [fk for fk in expected_fks if fk['constraint'] not in existing_names]

    if not missing_fks:
        print(f"\nAll {len(expected_fks)} FK constraints are present. Schema is correct.")
        sys.exit(0)

    print(f"\nFound {len(missing_fks)} MISSING FK constraints:")
    for fk in missing_fks:
        cols = ','.join(fk['columns'])
        print(f"  - {fk['constraint']}: {fk['table']}.{cols} -> {fk['ref_table']}")

    # Step 4: Fix orphans and create missing FKs
    print("\nRepairing missing constraints...")
    fixed = 0
    failed = 0

    for fk in missing_fks:
        if fix_and_create_fk(fk, db_params):
            fixed += 1
        else:
            failed += 1

    # Summary
    print(f"\n=== Verification Summary ===")
    print(f"  Expected FKs in dump:  {len(expected_fks)}")
    print(f"  Already present in DB: {len(expected_fks) - len(missing_fks)}")
    print(f"  Missing (repaired):    {fixed}")
    if failed:
        print(f"  Missing (FAILED):      {failed}")

    if failed > 0:
        print("\nWARNING: Some constraints could not be created. Manual intervention needed.")
        sys.exit(1)
    else:
        print("\nSchema verification complete. All FK constraints are present.")
        sys.exit(0)


if __name__ == '__main__':
    main()
