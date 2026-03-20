#!/bin/bash

# db-heal.sh - Ticketz Database Health Fix Script
#
# Cleans accumulated Baileys sessions, removes stale WebSocket sessions,
# reclaims Baileys TOAST bloat, and creates missing FK indexes.
#
# Safe to run on a live system:
#   - DELETE operations are instant (row-level)
#   - VACUUM FULL "Baileys" takes an exclusive lock for milliseconds (~20 rows)
#   - CREATE INDEX CONCURRENTLY does not lock the table
#
# Usage:
#   sidekick2 db-heal                  (via sidekick2 container)
#   bash /app/db-heal.sh               (directly)
#
# Called automatically by sidekick2 restore/import when --db-heal flag is used.

DB_NAME="${DB_NAME-ticketz}"
DB_USER="${DB_USER-ticketz}"
DB_HOST="${DB_HOST-postgres}"
DB_PORT="${DB_PORT-5432}"

# Helper: run psql with connection args
psql_run() {
    psql -h "${DB_HOST}" -p "${DB_PORT}" -U "${DB_USER}" -d "${DB_NAME}" "$@"
}

# Create a single-column FK index only if the column exists in the table.
# Handles schema differences between Ticketz versions (e.g. Lite vs Pro).
create_index_safe() {
    local table="$1"
    local column="$2"
    local indexname="$3"

    EXISTS=$(psql_run -t -c \
        "SELECT 1 FROM information_schema.columns WHERE table_name='${table}' AND column_name='${column}';" \
        2>/dev/null | tr -d ' \n')

    if [ "${EXISTS}" = "1" ]; then
        psql_run -c \
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS ${indexname} ON \"${table}\"(\"${column}\");" \
            2>/dev/null \
            && echo "  ok  ${indexname}" \
            || echo "  ERR ${indexname}"
    else
        echo "  --  ${indexname} (column '${column}' not in '${table}', skipped)"
    fi
}

# Create a partial index. Accepts the full ON ... WHERE ... definition.
# Reports success/failure without aborting the script.
create_partial_index_safe() {
    local indexname="$1"
    local definition="$2"

    psql_run -c \
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS ${indexname} ${definition};" \
        2>/dev/null \
        && echo "  ok  ${indexname}" \
        || echo "  ERR ${indexname} (failed - column may not exist in this version)"
}

# -------------------------------------------------------------------

echo ""
echo "==================================================================="
echo "  Ticketz DB Heal"
echo "==================================================================="
echo ""

# --- BEFORE stats ---
echo "--- Before ---"
psql_run -t << 'SQL'
SELECT '  BaileysKeys sessions    : ' || COUNT(*)         FROM "BaileysKeys" WHERE type = 'session';
SELECT '  UserSocketSessions old  : ' || COUNT(*)         FROM "UserSocketSessions" WHERE "createdAt" < NOW() - INTERVAL '2 hours';
SELECT '  Baileys table size      : ' || pg_size_pretty(pg_total_relation_size('"Baileys"'::regclass));
SELECT '  BaileysKeys table size  : ' || pg_size_pretty(pg_total_relation_size('"BaileysKeys"'::regclass));
SELECT '  FK without index        : ' || COUNT(*)
FROM information_schema.table_constraints tc
JOIN information_schema.key_column_usage kcu
  ON tc.constraint_name = kcu.constraint_name AND tc.table_schema = kcu.table_schema
JOIN information_schema.referential_constraints rc
  ON tc.constraint_name = rc.constraint_name AND tc.table_schema = rc.constraint_schema
WHERE tc.constraint_type = 'FOREIGN KEY'
  AND tc.table_schema = 'public'
  AND NOT EXISTS (
    SELECT 1 FROM pg_indexes i
    WHERE i.tablename = tc.table_name
      AND i.indexdef LIKE '%' || kcu.column_name || '%'
  );
SQL

# -------------------------------------------------------------------
# Step 1: Clean stale sessions
# -------------------------------------------------------------------
echo ""
echo "Step 1/5 - Cleaning stale sessions..."
psql_run << 'SQL'
DELETE FROM "BaileysKeys" WHERE type = 'session';
DELETE FROM "UserSocketSessions" WHERE "createdAt" < NOW() - INTERVAL '2 hours';
SQL
echo "  Done."

# -------------------------------------------------------------------
# Step 2: Configure aggressive autovacuum on Baileys
# The default threshold (50 rows) never triggers on this ~20-row table,
# causing TOAST bloat from frequent large contacts JSON updates.
# -------------------------------------------------------------------
echo ""
echo "Step 2/5 - Configuring autovacuum for Baileys..."
psql_run << 'SQL'
ALTER TABLE "Baileys" SET (
    autovacuum_vacuum_threshold     = 1,
    autovacuum_vacuum_scale_factor  = 0,
    autovacuum_vacuum_cost_delay    = 0,
    autovacuum_analyze_threshold    = 1,
    autovacuum_analyze_scale_factor = 0
);
SQL
echo "  Done."

# -------------------------------------------------------------------
# Step 3: Reclaim TOAST bloat from Baileys + VACUUM ANALYZE
# VACUUM FULL on Baileys takes an exclusive lock for milliseconds
# because the table has only ~20 rows of actual data.
# -------------------------------------------------------------------
echo ""
echo "Step 3/5 - Reclaiming Baileys TOAST bloat and running VACUUM..."
psql_run << 'SQL'
SET maintenance_work_mem = '32MB';
VACUUM FULL "Baileys";
VACUUM ANALYZE "Baileys";
VACUUM ANALYZE "BaileysKeys";
VACUUM ANALYZE "UserSocketSessions";
VACUUM ANALYZE "Messages";
VACUUM ANALYZE "Tickets";
VACUUM ANALYZE "TicketTraking";
SQL
echo "  Done."

# -------------------------------------------------------------------
# Step 4: Create missing FK indexes
# CONCURRENTLY = no table locks, safe on live systems.
# IF NOT EXISTS = idempotent, safe to re-run.
# create_index_safe = skips if column absent (version compatibility).
# -------------------------------------------------------------------
echo ""
echo "Step 4/5 - Creating missing FK indexes (CONCURRENTLY, no downtime)..."

# Tickets
create_index_safe "Tickets" "companyId"     "idx_tickets_companyid"
create_index_safe "Tickets" "contactId"     "idx_tickets_contactid"
create_index_safe "Tickets" "userId"        "idx_tickets_userid"
create_index_safe "Tickets" "whatsappId"    "idx_tickets_whatsappid"
create_index_safe "Tickets" "queueId"       "idx_tickets_queueid"
create_index_safe "Tickets" "queueOptionId" "idx_tickets_queueoptionid"

# TicketTraking
create_index_safe "TicketTraking" "companyId"  "idx_tickettraking_companyid"
create_index_safe "TicketTraking" "userId"     "idx_tickettraking_userid"
create_index_safe "TicketTraking" "whatsappId" "idx_tickettraking_whatsappid"

# TicketTraking partial indexes (match specific queries used by the system)
create_partial_index_safe "idx_tickettraking_rating_job" \
    'ON "TicketTraking"("ticketId") WHERE rated = false AND expired = false AND "ratingAt" IS NOT NULL'
create_partial_index_safe "idx_tickettraking_open" \
    'ON "TicketTraking"("ticketId") WHERE "finishedAt" IS NULL'
create_partial_index_safe "idx_tickettraking_unrated" \
    'ON "TicketTraking"("ticketId") WHERE rated = false'
create_partial_index_safe "idx_tickettraking_unexpired" \
    'ON "TicketTraking"("ticketId") WHERE expired = false'
create_partial_index_safe "idx_tickettraking_pending_rating" \
    'ON "TicketTraking"("ratingAt") WHERE "ratingAt" IS NOT NULL AND rated = false'

# Messages
create_index_safe "Messages" "userId"  "idx_messages_userid"
create_index_safe "Messages" "queueId" "idx_messages_queueid"

# Contacts
create_index_safe "Contacts" "companyId" "idx_contacts_companyid"

# ContactCustomFields
create_index_safe "ContactCustomFields" "contactId" "idx_contactcustomfields_contactid"

# UserSocketSessions
create_index_safe "UserSocketSessions" "userId" "idx_usersocketsessions_userid"

# Settings
create_index_safe "Settings" "companyId" "idx_settings_companyid"

# Users
create_index_safe "Users" "companyId" "idx_users_companyid"

# Schedules (userId and whatsappId absent in Lite version - create_index_safe handles this)
create_index_safe "Schedules" "contactId"  "idx_schedules_contactid"
create_index_safe "Schedules" "ticketId"   "idx_schedules_ticketid"
create_index_safe "Schedules" "userId"     "idx_schedules_userid"
create_index_safe "Schedules" "whatsappId" "idx_schedules_whatsappid"
create_index_safe "Schedules" "queueId"    "idx_schedules_queueid"

# Queues
create_index_safe "Queues" "whatsappId" "idx_queues_whatsappid"
create_index_safe "Queues" "tagId"      "idx_queues_tagid"

# QueueOptions
create_index_safe "QueueOptions" "queueId"        "idx_queueoptions_queueid"
create_index_safe "QueueOptions" "parentId"       "idx_queueoptions_parentid"
create_index_safe "QueueOptions" "tagId"          "idx_queueoptions_tagid"
create_index_safe "QueueOptions" "forwardQueueId" "idx_queueoptions_forwardqueueid"

# TicketNotes
create_index_safe "TicketNotes" "ticketId"  "idx_ticketnotes_ticketid"
create_index_safe "TicketNotes" "userId"    "idx_ticketnotes_userid"
create_index_safe "TicketNotes" "contactId" "idx_ticketnotes_contactid"

# UserRatings
create_index_safe "UserRatings" "ticketId"  "idx_userratings_ticketid"
create_index_safe "UserRatings" "userId"    "idx_userratings_userid"
create_index_safe "UserRatings" "companyId" "idx_userratings_companyid"

# Campaigns and related
create_index_safe "Campaigns"        "companyId"      "idx_campaigns_companyid"
create_index_safe "Campaigns"        "whatsappId"     "idx_campaigns_whatsappid"
create_index_safe "Campaigns"        "contactListId"  "idx_campaigns_contactlistid"
create_index_safe "Campaigns"        "tagId"          "idx_campaigns_tagid"
create_index_safe "Campaigns"        "exclusionTagId" "idx_campaigns_exclusiontagid"
create_index_safe "CampaignShipping" "contactId"      "idx_campaignshipping_contactid"
create_index_safe "CampaignShipping" "tagContactId"   "idx_campaignshipping_tagcontactid"
create_index_safe "CampaignSettings" "companyId"      "idx_campaignsettings_companyid"
create_index_safe "ContactLists"     "companyId"      "idx_contactlists_companyid"
create_index_safe "ContactListItems" "companyId"      "idx_contactlistitems_companyid"

# Chats
create_index_safe "Chats" "companyId" "idx_chats_companyid"
create_index_safe "Chats" "ownerId"   "idx_chats_ownerid"

# ChatMessages
create_index_safe "ChatMessages" "chatId"   "idx_chatmessages_chatid"
create_index_safe "ChatMessages" "senderId" "idx_chatmessages_senderid"

# ChatUsers
create_index_safe "ChatUsers" "chatId" "idx_chatusers_chatid"
create_index_safe "ChatUsers" "userId" "idx_chatusers_userid"

# Integrations
create_index_safe "Integrations"        "queueId"       "idx_integrations_queueid"
create_index_safe "IntegrationSessions" "integrationId" "idx_integrationsessions_integrationid"
create_index_safe "IntegrationSessions" "ticketId"      "idx_integrationsessions_ticketid"

# Others
create_index_safe "Invoices"             "companyId"  "idx_invoices_companyid"
create_index_safe "Subscriptions"        "companyId"  "idx_subscriptions_companyid"
create_index_safe "Announcements"        "companyId"  "idx_announcements_companyid"
create_index_safe "QuickMessages"        "companyId"  "idx_quickmessages_companyid"
create_index_safe "QuickMessages"        "userId"     "idx_quickmessages_userid"
create_index_safe "QuickPix"             "companyId"  "idx_quickpix_companyid"
create_index_safe "Companies"            "planId"     "idx_companies_planid"
create_index_safe "Funnels"              "companyId"  "idx_funnels_companyid"
create_index_safe "FunnelSteps"          "funnelId"   "idx_funnelsteps_funnelid"
create_index_safe "OutOfTicketMessages"  "whatsappId" "idx_outtickermessages_whatsappid"
create_index_safe "OldMessages"          "ticketId"   "idx_oldmessages_ticketid"
create_index_safe "OldMessages"          "userId"     "idx_oldmessages_userid"
create_index_safe "WhatsappLidMaps"      "contactId"  "idx_whatsapplid_contactid"
create_index_safe "WebpushSubscriptions" "userId"     "idx_webpushsubscriptions_userid"

echo ""
echo "Step 5/5 - Updating statistics..."
psql_run -c "VACUUM ANALYZE;" 2>/dev/null
echo "  Done."

# --- AFTER stats ---
echo ""
echo "--- After ---"
psql_run -t << 'SQL'
SELECT '  BaileysKeys sessions    : ' || COUNT(*)         FROM "BaileysKeys" WHERE type = 'session';
SELECT '  UserSocketSessions old  : ' || COUNT(*)         FROM "UserSocketSessions" WHERE "createdAt" < NOW() - INTERVAL '2 hours';
SELECT '  Baileys table size      : ' || pg_size_pretty(pg_total_relation_size('"Baileys"'::regclass));
SELECT '  BaileysKeys table size  : ' || pg_size_pretty(pg_total_relation_size('"BaileysKeys"'::regclass));
SELECT '  FK without index        : ' || COUNT(*)
FROM information_schema.table_constraints tc
JOIN information_schema.key_column_usage kcu
  ON tc.constraint_name = kcu.constraint_name AND tc.table_schema = kcu.table_schema
JOIN information_schema.referential_constraints rc
  ON tc.constraint_name = rc.constraint_name AND tc.table_schema = rc.constraint_schema
WHERE tc.constraint_type = 'FOREIGN KEY'
  AND tc.table_schema = 'public'
  AND NOT EXISTS (
    SELECT 1 FROM pg_indexes i
    WHERE i.tablename = tc.table_name
      AND i.indexdef LIKE '%' || kcu.column_name || '%'
  );
SQL

echo ""
echo "==================================================================="
echo "  DB Heal completed."
echo ""
echo "  To flush in-memory sessions from the backend, restart it:"
echo "    docker compose restart ticketz-docker-acme-backend-1"
echo "==================================================================="
echo ""
