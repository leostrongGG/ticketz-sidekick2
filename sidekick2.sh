#!/bin/bash

# ticketz-sidekick2
# Enhanced backup/restore tool for Ticketz with company-level filtering
# Based on ticketz-sidekick, adding --companies support

# Configuration variables
BACKUP_DIR="/backups"          # Directory visible outside the container
DATA_DIRS=("/backend-public" "/backend-private")  # List of directories with the files
DB_NAME="${DB_NAME-ticketz}"           # Database name
DB_USER="${DB_USER-ticketz}"           # Database user
DB_HOST="${DB_HOST-postgres}"          # Database host
DB_PORT="${DB_PORT-5432}"              # Database port
TIMESTAMP=$(date +"%Y%m%d%H%M%S")
BACKUP_BASENAME="ticketz-backup"
BACKUP_FILE="${BACKUP_DIR}/${BACKUP_BASENAME}-${TIMESTAMP}.tar.gz"
RETENTION_FILES=${RETENTION_FILES-7}           # Number of files to keep

# Wait for postgres to be available
wait_for_postgres() {
    for i in {1..30}
    do
        if psql -h "${DB_HOST}" -U "${DB_USER}" -d "${DB_NAME}" -c '\q' -q; then
            echo "Postgres is up - executing command"
			return
        else
            echo "Postgres is unavailable - sleeping"
            sleep 1
        fi
    done

    echo "Postgres is still unavailable after 30 seconds - exiting"
    exit 1
}

# Database and folders backup function
backup() {
    # Parse parameters
    DBONLY=0
    COMPANIES=""
    while [ $# -gt 0 ]; do
        case "$1" in
            --dbonly)
                echo "Will backup only the database"
                DBONLY=1
                ;;
            --companies)
                shift
                COMPANIES="$1"
                echo "Will filter backup for companies: ${COMPANIES} (+ company 1/admin)"
                ;;
        esac
        shift
    done

    # Wait for Postgres to become available
    wait_for_postgres

    echo "Creating database dump..."
    # Postgres database dump
    pg_dump -h "${DB_HOST}" -p "${DB_PORT}" -U "${DB_USER}" "${DB_NAME}" > "${BACKUP_DIR}/db_dump.sql"

    # Filter for specific companies if requested
    if [ -n "${COMPANIES}" ]; then
        echo "Filtering dump for companies: ${COMPANIES}..."
        MEDIA_LIST="${BACKUP_DIR}/_media_keep.txt"
        python3 /app/ticketz-filter.py "${BACKUP_DIR}/db_dump.sql" "${COMPANIES}" --media-list "${MEDIA_LIST}"

        if [ $? -ne 0 ]; then
            echo "ERROR: Filter failed. Aborting backup."
            rm -f "${BACKUP_DIR}/db_dump.sql" "${MEDIA_LIST}"
            exit 1
        fi

        # Filter media files in data directories (keep only referenced files)
        if [ $DBONLY -eq 0 ] && [ -f "${MEDIA_LIST}" ]; then
            # Build a set of basenames to keep (single pass with awk - fast)
            KEEP_NAMES=$(mktemp)
            sed 's:.*/::' "${MEDIA_LIST}" | sort -u > "$KEEP_NAMES"

            for dir in "${DATA_DIRS[@]}"; do
                if [ -d "$dir" ]; then
                    echo "Filtering media in ${dir}..."
                    ORIG_COUNT=$(find "$dir" -type f | wc -l)

                    # List all files as "basename\tfullpath", remove those not in keep set
                    # awk loads keep set into memory (O(n+m) instead of O(n*m) find calls)
                    FILES_TO_REMOVE=$(mktemp)
                    find "$dir" -type f -printf '%f\t%p\n' | \
                        awk -F'\t' 'NR==FNR{keep[$1]=1; next} !($1 in keep){print $2}' \
                        "$KEEP_NAMES" - > "$FILES_TO_REMOVE"
                    REMOVED=$(wc -l < "$FILES_TO_REMOVE")
                    xargs -r -d '\n' rm -f < "$FILES_TO_REMOVE"
                    rm -f "$FILES_TO_REMOVE"

                    KEPT_COUNT=$(find "$dir" -type f | wc -l)
                    echo "  ${dir}: ${ORIG_COUNT} files -> ${KEPT_COUNT} kept (${REMOVED} removed)"
                fi
            done
            rm -f "$KEEP_NAMES"
        fi
        rm -f "${MEDIA_LIST}"
    fi

    echo "DBONLY = ${DBONLY}"
    if [ $DBONLY -eq 1 ]; then
        # Only backup the database dump
        tar -czf "${BACKUP_FILE}" "${BACKUP_DIR}/db_dump.sql"
    else
        # Backup database dump and data directories
        echo "Backing up data directories: ${DATA_DIRS[*]}"
        tar -czf "${BACKUP_FILE}" "${BACKUP_DIR}/db_dump.sql" $(printf " %s" "${DATA_DIRS[@]}")
    fi

    # Remove the sql dump after compressing
    rm "${BACKUP_DIR}/db_dump.sql"

    echo "Backup completed: ${BACKUP_FILE}"

    # Cleanup of old backups
    cleanup
}

# Function to restore the database and files
restore() {
  
    # Check if there are backup files
    if [ -z "$(ls -A ${BACKUP_DIR}/${BACKUP_BASENAME}-*.tar.gz 2>/dev/null)" ]; then
        echo "No backup files found. Exiting."
        exit 1
    fi

    LATEST_BACKUP=$(ls -t ${BACKUP_DIR}/${BACKUP_BASENAME}-*.tar.gz | head -n 1)

    # Wait for Postgres to become available
    wait_for_postgres

    # Check if the database is empty
    DB_COUNT=$(psql -h "${DB_HOST}" -U "${DB_USER}" -d "${DB_NAME}" -t -c "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public';" -q)

    if [ "${DB_COUNT}" -gt 0 ]; then
        echo "The database already has tables. Will not restore."
        return
    fi

    DBONLY=0
    # Check if backup file has data directories
    for dir in "${DATA_DIRS[@]}"; do
        if ! tar -tzf "$LATEST_BACKUP" | grep -q "^${dir#/}"; then
            echo "Backup file have only the database dump"
            DBONLY=1
            break
        fi
    done

    # Check if the directories are empty
    for dir in "${DATA_DIRS[@]}"; do
        if [ "$(ls -A ${dir})" ] && [ $DBONLY -eq 0 ] ; then
            echo "Directory ${dir} is not empty. Will not restore."
            return
        fi
    done

    echo "Starting restoration..."

    # Restore files from the last backup
    tar -xzf $(ls -t ${LATEST_BACKUP} | head -n 1) -C / || exit 1

    echo "Restoring database..."
    psql -h "${DB_HOST}" -U "${DB_USER}" -d "${DB_NAME}" -q < "${BACKUP_DIR}/db_dump.sql" &> /dev/null || exit 1

    # Verify and repair FK constraints that may have failed silently
    echo ""
    echo "Verifying schema integrity..."
    python3 /app/ticketz-verify.py "${BACKUP_DIR}/db_dump.sql" \
        --db-host "${DB_HOST}" \
        --db-name "${DB_NAME}" \
        --db-user "${DB_USER}" \
        --db-port "${DB_PORT}"

    VERIFY_EXIT=$?

    # Clean up dump file
    rm -f "${BACKUP_DIR}/db_dump.sql"

    if [ $VERIFY_EXIT -ne 0 ]; then
        echo ""
        echo "WARNING: Schema verification found issues that could not be auto-fixed."
        echo "  The database was restored but may have missing FK constraints."
        echo "  Check the output above for details."
    fi

    echo "Restoration completed."
}


# Function to import a single company into an existing database
import_company() {
    IMPORT_FILE=""
    DRY_RUN=""
    while [ $# -gt 0 ]; do
        case "$1" in
            --dry-run)
                DRY_RUN="--dry-run"
                ;;
            *)
                if [ -z "${IMPORT_FILE}" ]; then
                    IMPORT_FILE="$1"
                fi
                ;;
        esac
        shift
    done

    if [ -z "${IMPORT_FILE}" ]; then
        echo ""
        echo "Usage: sidekick2 import <backup.tar.gz> [--dry-run]"
        echo ""
        echo "  Import a single company from a filtered backup into an existing"
        echo "  Ticketz database. The backup must contain exactly ONE company"
        echo "  (besides company 1/admin)."
        echo ""
        echo "  All IDs are remapped to avoid conflicts with existing data."
        echo "  The operation is wrapped in a single transaction — if anything"
        echo "  fails, the database is NOT modified (automatic rollback)."
        echo ""
        echo "Options:"
        echo "  --dry-run    Generate the import SQL without executing it."
        echo "               The SQL is saved to /backups/ for inspection."
        echo ""
        exit 1
    fi

    if [ ! -f "${IMPORT_FILE}" ]; then
        echo "ERROR: File not found: ${IMPORT_FILE}"
        exit 1
    fi

    # Wait for Postgres to become available
    wait_for_postgres

    # Check if the database has tables (must NOT be empty for import)
    DB_COUNT=$(psql -h "${DB_HOST}" -U "${DB_USER}" -d "${DB_NAME}" -t -c \
        "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public';" -q | tr -d ' ')

    if [ "${DB_COUNT}" -eq 0 ] 2>/dev/null; then
        echo "ERROR: Database is empty. Use 'restore' instead of 'import'."
        echo "  Import is for adding a company to an EXISTING installation."
        echo "  Restore is for setting up a NEW installation from backup."
        exit 1
    fi

    # Warn about backend
    echo ""
    echo "==================================================================="
    echo "  IMPORTANT: The Ticketz backend MUST be stopped before importing."
    echo ""
    echo "  This prevents ID conflicts from concurrent operations (e.g.,"
    echo "  new messages arriving while IDs are being reassigned)."
    echo ""
    echo "  From your ticketz project folder, run:"
    echo "    cd ~/ticketz-docker-acme && docker compose stop ticketz-docker-acme-backend-1"
    echo "  or:"
    echo "    cd ~/ticketz-docker-acme && docker compose down"
    echo ""
    echo "  After import, restart with:"
    echo "    docker compose up -d"
    echo "==================================================================="
    echo ""
    read -p "Is the backend stopped? Continue? (y/n): " CONFIRM
    if [ "$CONFIRM" != "y" ] && [ "$CONFIRM" != "Y" ]; then
        echo "Aborted. Stop the backend and try again."
        exit 0
    fi

    # Create temp directory for extraction
    TMPDIR=$(mktemp -d)
    echo ""
    echo "Extracting backup to ${TMPDIR}..."
    tar -xzf "${IMPORT_FILE}" -C "${TMPDIR}"

    # Find the dump file
    DUMP_FILE=""
    if [ -f "${TMPDIR}/backups/db_dump.sql" ]; then
        DUMP_FILE="${TMPDIR}/backups/db_dump.sql"
    else
        DUMP_FILE=$(find "${TMPDIR}" -name "db_dump.sql" -type f | head -1)
    fi

    if [ -z "${DUMP_FILE}" ] || [ ! -f "${DUMP_FILE}" ]; then
        echo "ERROR: db_dump.sql not found in backup."
        rm -rf "${TMPDIR}"
        exit 1
    fi
    echo "Found dump: ${DUMP_FILE}"

    # Create safety backup of current database
    if [ -z "${DRY_RUN}" ]; then
        echo ""
        echo "Creating safety backup of current database..."
        SAFETY_BACKUP="${BACKUP_DIR}/pre-import-${TIMESTAMP}.sql.gz"
        pg_dump -h "${DB_HOST}" -p "${DB_PORT}" -U "${DB_USER}" "${DB_NAME}" | gzip > "${SAFETY_BACKUP}"
        SAFETY_SIZE=$(du -h "${SAFETY_BACKUP}" | cut -f1)
        echo "Safety backup saved: ${SAFETY_BACKUP} (${SAFETY_SIZE})"
        echo "  If import fails, restore with:"
        echo "    gunzip -c ${SAFETY_BACKUP} | psql -h ${DB_HOST} -U ${DB_USER} -d ${DB_NAME}"
    fi

    # Determine media paths
    MEDIA_SRC="${TMPDIR}/backend-public"
    MEDIA_DST="/backend-public"
    MEDIA_ARGS=""
    if [ -d "${MEDIA_SRC}/media" ]; then
        MEDIA_ARGS="--media-src ${MEDIA_SRC} --media-dst ${MEDIA_DST}"
    fi

    OUTPUT_SQL="${TMPDIR}/import.sql"
    MEDIA_MAP="${TMPDIR}/media_map.json"

    echo ""
    echo "Running import analysis and ID remapping..."
    python3 /app/ticketz-import.py "${DUMP_FILE}" \
        --db-host "${DB_HOST}" \
        --db-name "${DB_NAME}" \
        --db-user "${DB_USER}" \
        --db-port "${DB_PORT}" \
        --output "${OUTPUT_SQL}" \
        --media-map "${MEDIA_MAP}" \
        ${MEDIA_ARGS} \
        ${DRY_RUN}

    IMPORT_EXIT=$?

    if [ $IMPORT_EXIT -ne 0 ]; then
        echo ""
        echo "ERROR: Import analysis failed."
        rm -rf "${TMPDIR}"
        exit 1
    fi

    if [ -n "${DRY_RUN}" ]; then
        # Copy preview files to backups for inspection
        echo ""
        cp "${OUTPUT_SQL}" "${BACKUP_DIR}/import-preview-${TIMESTAMP}.sql"
        cp "${MEDIA_MAP}" "${BACKUP_DIR}/import-media-map-${TIMESTAMP}.json" 2>/dev/null
        echo "DRY RUN complete. Files saved for inspection:"
        echo "  SQL:       ${BACKUP_DIR}/import-preview-${TIMESTAMP}.sql"
        echo "  Media map: ${BACKUP_DIR}/import-media-map-${TIMESTAMP}.json"
        echo ""
        echo "Review the SQL file, then run without --dry-run to execute."
        rm -rf "${TMPDIR}"
        exit 0
    fi

    # Execute the import SQL
    echo ""
    echo "Executing import SQL (single transaction)..."
    psql -h "${DB_HOST}" -p "${DB_PORT}" -U "${DB_USER}" -d "${DB_NAME}" \
        --single-transaction -q < "${OUTPUT_SQL}" 2>&1

    if [ $? -ne 0 ]; then
        echo ""
        echo "ERROR: SQL import failed. Transaction was ROLLED BACK."
        echo "  The database was NOT modified."
        echo "  Safety backup: ${SAFETY_BACKUP}"
        echo ""
        echo "  The generated SQL is saved for debugging:"
        cp "${OUTPUT_SQL}" "${BACKUP_DIR}/import-failed-${TIMESTAMP}.sql"
        echo "  ${BACKUP_DIR}/import-failed-${TIMESTAMP}.sql"
        rm -rf "${TMPDIR}"
        exit 1
    fi

    echo "SQL import successful!"

    # Cleanup
    rm -rf "${TMPDIR}"

    echo ""
    echo "==================================================================="
    echo "  IMPORT COMPLETED SUCCESSFULLY!"
    echo ""
    echo "  Remember to start the backend:"
    echo "    docker compose up -d"
    echo ""
    echo "  Safety backup: ${SAFETY_BACKUP}"
    echo "==================================================================="
    echo ""
}


# Function for cleanup of old backups
cleanup() {
    echo "Running cleanup of old backups..."

    # List all backup files, sort by modification time, and remove files exceeding the retention limit
    ls -t ${BACKUP_DIR}/${BACKUP_BASENAME}-*.tar.gz | tail -n +$((${RETENTION_FILES} + 1)) | /usr/bin/xargs -d '\n' rm -f --

    echo "Cleanup completed."
}

# Choice of operation according to the passed command
case "$1" in
    backup)
        shift
        backup $*
        ;;
    restore)
        restore
        ;;
    import)
        shift
        import_company $*
        ;;
    *)
        echo "Unrecognized command. Use 'backup', 'restore' or 'import'."
        exit 1
        ;;
esac
