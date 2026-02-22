# ticketz-sidekick2

Enhanced backup/restore tool for [Ticketz](https://ticke.tz) with company-level filtering.

Based on the original [ticketz-sidekick](https://github.com/ticketz-oss/ticketz-sidekick), adding the ability to backup and restore specific companies from a multi-tenant Ticketz installation.

## Features

Everything from the original sidekick, plus:

- **`--companies`** — Filter backup to include only specified companies
- Company 1 (admin) is always included automatically (required by Ticketz)
- Company 1's WhatsApp connections are excluded to avoid session conflicts
- Media files are filtered to include only referenced files
- Compatible with the standard sidekick restore process

## Usage

### Filtered Backup (new)

```bash
# Backup only company 263 (+ company 1/admin)
sudo docker compose run --rm sidekick backup --companies 263

# Multiple companies
sudo docker compose run --rm sidekick backup --companies 263,10,45

# Range of companies
sudo docker compose run --rm sidekick backup --companies 10-50

# Mixed
sudo docker compose run --rm sidekick backup --companies 263,10-20,45

# Database only (no media files)
sudo docker compose run --rm sidekick backup --companies 263 --dbonly
```

### Standard Commands (unchanged)

```bash
# Full backup (all companies)
sudo docker compose run --rm sidekick backup

# Database-only backup
sudo docker compose run --rm sidekick backup --dbonly

# Restore latest backup
sudo docker compose run --rm sidekick restore

# Retrieve tables from another database
sudo docker compose run --rm sidekick retrieve <dbhost> <dbname> <dbuser> [dbpass]
```

## Docker Compose

Replace the sidekick image in your `docker-compose.yaml`:

```yaml
services:
  sidekick:
    build: ./sidekick2
    # or use: image: ghcr.io/seu-usuario/ticketz-sidekick2:latest
    volumes:
      - ./backups:/backups
      - backend-public:/backend-public
      - backend-private:/backend-private
    environment:
      - DB_NAME=ticketz
      - DB_USER=ticketz
      - DB_HOST=postgres
      - DB_PORT=5432
      - RETENTION_FILES=7
```

## How Company Filtering Works

1. `pg_dump` generates the full database dump (same as original)
2. `ticketz-filter.py` performs a two-pass filter:
   - **Pass 1**: Reads the dump collecting IDs (contacts, tickets, users, etc.) for the selected companies
   - **Pass 2**: Writes a new dump keeping only rows belonging to those companies
3. Media files in `/backend-public` and `/backend-private` are filtered to keep only files referenced in the filtered database
4. The filtered dump and media are packaged into a standard `ticketz-backup-*.tar.gz`

### Company 1 (Admin)

Company 1 is the system/admin company in Ticketz. It's **always included** because:
- `GetSuperSettingService` is hardcoded to use `companyId: 1`
- `CheckCompanyCompliant` treats company 1 as always compliant
- Default seeds create company 1 with essential system settings

However, company 1's **WhatsApp connections are excluded** to prevent session conflicts when restoring on a different server.

## Tables Classification

| Category | Tables | Filter Method |
|---|---|---|
| **Global** | Plans, Helps, Translations, SequelizeMeta, SequelizeData | No filter (keep all) |
| **Direct** | Contacts, Tickets, Messages, Users, Queues, Whatsapps, +16 more | Filter by `companyId` |
| **Indirect** | Baileys, BaileysKeys, ChatMessages, ContactTags, TicketTags, +16 more | Filter by FK → collected IDs |
| **Company** | Companies | Filter by `id` |

## License

MIT
