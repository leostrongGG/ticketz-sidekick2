# 🔧 ticketz-sidekick2

Ferramenta avançada de backup, restore e importação de empresas para o [Ticketz](https://github.com/ticketz-oss/ticketz) (sistema multi-tenant de atendimento via WhatsApp).

## 📋 Sobre

Baseado no [ticketz-sidekick](https://github.com/ticketz-oss/ticketz-sidekick) original, o sidekick2 adiciona recursos para gerenciar backups filtrados por empresa e importar empresas entre instalações Ticketz diferentes.

- ✅ Backup filtrado por empresa(s) específica(s) com `--companies`
- ✅ Importação de empresa para instalação existente com remapeamento completo de IDs
- ✅ Company 1 (admin/sistema) sempre incluída automaticamente
- ✅ Conexões WhatsApp da company 1 excluídas para evitar conflitos de sessão
- ✅ Mídias filtradas para incluir apenas arquivos referenciados
- ✅ Compatível com o processo de restore padrão do sidekick e auto-instalador
- ✅ Roda como container **separado** ao lado da instalação Ticketz existente

## 🏗️ Arquitetura

```
┌─────────────────────────────────────────────────────────┐
│  PostgreSQL (Container Ticketz)                         │
│  └─ 51 tabelas · multi-tenant por companyId             │
└─────────────────────────────────────────────────────────┘
                         ↓
┌─────────────────────────────────────────────────────────┐
│  sidekick2 (Container separado)                         │
│  ├─ sidekick2.sh        (orquestra backup/restore/import│
│  ├─ ticketz-filter.py   (filtra dump por empresa)       │
│  └─ ticketz-import.py   (remapeia IDs para importação)  │
└─────────────────────────────────────────────────────────┘
         ↓                ↓                ↓
     [backup]         [restore]        [import]
    pg_dump →          tar.gz →        backup →
    filter →          psql load       scan IDs →
    tar.gz                            remap IDs →
                                      transação SQL
```

## 📦 Estrutura do Projeto

```
ticketz-sidekick2/
├── sidekick2.sh            # Script principal (backup/restore/import)
├── ticketz-filter.py       # Filtro de dump SQL por empresa (2 passagens)
├── ticketz-import.py       # Importação com remapeamento de IDs
├── Dockerfile              # Container baseado em postgres:16-alpine
├── docker-compose.yaml     # Configuração standalone com rede/volumes externos
├── README.md               # Esta documentação
├── CONTRIBUTING.md         # Guia de contribuição
├── LICENSE                 # Licença MIT
└── backups/                # Backups gerados (gitignored)
```

## 🚀 Instalação

### Pré-requisitos

- Linux com Docker e Docker Compose
- Ticketz instalado (auto-instalador ou manual)
- Acesso ao servidor via SSH

### Setup

1. **Clone o repositório** (na mesma pasta pai do Ticketz)

```
~/
  ticketz-docker-acme/   ← instalação Ticketz existente (padrão auto-instalador)
  ticketz-sidekick2/     ← este projeto
```

```bash
cd ~
git clone https://github.com/leostronggg/ticketz-sidekick2.git
cd ticketz-sidekick2
mkdir -p backups
```

2. **Ajuste o prefixo da rede/volumes** (se necessário)

O `docker-compose.yaml` usa `ticketz-docker-acme_` como prefixo (padrão do auto-instalador).
Se sua pasta do Ticketz tem outro nome, ajuste o prefixo:

```bash
# Verificar o nome correto
docker network ls | grep ticketz

# Se necessário, editar docker-compose.yaml e trocar o prefixo
```

3. **Build do container**

```bash
docker compose build
```

## 💡 Uso

### 1️⃣ Backup Filtrado por Empresa

```bash
# Backup apenas da empresa 263 (company 1/admin sempre incluída)
docker compose run --rm sidekick2 backup --companies 263

# Múltiplas empresas
docker compose run --rm sidekick2 backup --companies 263,10,45

# Range de empresas
docker compose run --rm sidekick2 backup --companies 10-50

# Misto
docker compose run --rm sidekick2 backup --companies 263,10-20,45

# Apenas banco de dados (sem mídias)
docker compose run --rm sidekick2 backup --companies 263 --dbonly
```

### 2️⃣ Backup/Restore Padrão

```bash
# Backup completo (todas as empresas)
docker compose run --rm sidekick2 backup

# Backup apenas banco de dados
docker compose run --rm sidekick2 backup --dbonly

# Restore do último backup (banco deve estar VAZIO)
docker compose run --rm sidekick2 restore
```

Os backups são salvos em `./backups/` no host.

### 3️⃣ Import (adicionar empresa a uma instalação existente)

Importa uma empresa de um backup filtrado para um banco Ticketz ativo.
Todos os IDs são remapeados para evitar conflitos. A operação é envolvida
em uma transação PostgreSQL — se qualquer coisa falhar, o banco **NÃO é modificado**.

```bash
# Primeiro, crie um backup filtrado com UMA empresa:
docker compose run --rm sidekick2 backup --companies 263

# Preview da importação (gera SQL sem executar):
docker compose run --rm sidekick2 import /backups/ticketz-backup-XXXXX.tar.gz --dry-run

# Executar a importação:
docker compose run --rm sidekick2 import /backups/ticketz-backup-XXXXX.tar.gz
```

#### Antes de importar

1. **Pare o backend do Ticketz** para evitar conflitos de IDs:
   ```bash
   cd ~/ticketz-docker-acme && docker compose stop ticketz-docker-acme-backend-1
   ```
2. O script pede confirmação antes de prosseguir
3. Um backup de segurança do banco atual é criado automaticamente
4. Sempre use `--dry-run` primeiro para revisar as mudanças

#### O que acontece durante o import

1. Backup é extraído e validado (deve ter exatamente 1 empresa além da company 1)
2. Banco destino é verificado como não-vazio (se vazio, use `restore`)
3. IDs máximos atuais são lidos do banco destino
4. Todos os IDs no dump são remapeados para novos valores sequenciais:
   - `companyId`, `userId`, `contactId`, `ticketId`, `queueId`, `whatsappId`
   - `tagId`, `funnelId`, `chatId`, `campaignId`, `contactListId`
   - `parentId` (auto-referência em QueueOptions)
5. Caminhos de mídia no banco são atualizados (`media/{oldCID}/...` → `media/{newCID}/...`)
6. Arquivos de mídia são copiados com estrutura de diretórios remapeada
7. SQL remapeado é executado em transação única (`BEGIN`/`COMMIT`)
8. Sequences do PostgreSQL são atualizadas (`setval`)

## 🔄 Como Funciona o Filtro por Empresa

1. `pg_dump` gera o dump completo do banco
2. `ticketz-filter.py` executa um filtro em duas passagens:
   - **Passagem 1**: Lê o dump coletando IDs (contatos, tickets, usuários, etc.) das empresas selecionadas
   - **Passagem 2**: Escreve um novo dump mantendo apenas linhas pertencentes às empresas
3. Mídias em `/backend-public` e `/backend-private` são filtradas para manter apenas arquivos referenciados
4. O dump filtrado e mídias são empacotados em `ticketz-backup-*.tar.gz`

O `.tar.gz` resultante é totalmente compatível com o restore padrão do sidekick e do auto-instalador.

### Company 1 (Admin/Sistema)

A company 1 é a empresa de sistema do Ticketz. É **sempre incluída** porque:
- `GetSuperSettingService` é hardcoded para usar `companyId: 1`
- `CheckCompanyCompliant` trata company 1 como sempre em conformidade
- Seeds padrão criam a company 1 com configurações essenciais

Porém, as **conexões WhatsApp da company 1 são excluídas** para evitar conflitos de sessão ao restaurar em outro servidor.

## 📊 Classificação das Tabelas

| Categoria | Tabelas | Método de Filtro |
|---|---|---|
| **Globais** | Plans, Helps, Translations, SequelizeMeta, SequelizeData | Sem filtro (mantém tudo) |
| **Diretas** | Contacts, Tickets, Messages, Users, Queues, Whatsapps, +16 | Filtro por `companyId` |
| **Indiretas** | Baileys, BaileysKeys, ChatMessages, ContactTags, TicketTags, +16 | Filtro por FK → IDs coletados |
| **Empresa** | Companies | Filtro por `id` |

## ⚠️ Avisos Importantes

- **S3/Storage remoto**: Se a instalação de origem usava S3 para mídia, use um **bucket diferente** no destino para evitar conflitos. URLs de S3 no banco **NÃO são remapeadas**.
- **Plans**: O `planId` no registro da empresa deve corresponder a um Plan válido no banco destino.
- **Sessões WhatsApp**: Dados do Baileys são importados mas podem não funcionar no novo servidor (sessões são específicas do dispositivo).
- **Dados da company 1** do backup são **ignorados** no import (o destino já tem sua própria company 1).

## 🛡️ Segurança

- ✅ **Transação atômica** — qualquer erro no import causa ROLLBACK total
- ✅ **`--dry-run`** — gera SQL para inspeção sem executar
- ✅ **Backup de segurança** criado automaticamente antes do import
- ✅ **`session_replication_role = replica`** — desabilita triggers FK durante import
- ✅ **Validações** — verifica empresa única, banco não-vazio, backend parado
- ✅ **Compatível** com restore padrão e auto-instalador

## 🔧 Troubleshooting

### Erro de rede/volume não encontrado

```bash
# Verificar nomes corretos
docker network ls | grep ticketz
docker volume ls | grep ticketz

# O prefixo deve corresponder ao docker-compose.yaml
```

### Erro de permissão no backup

```bash
# Verificar se a pasta backups existe e tem permissão
mkdir -p backups
chmod 755 backups
```

### Verificar containers do Ticketz

```bash
docker ps | grep ticketz
```

## 🤝 Contribuindo

Contribuições são bem-vindas! Veja [CONTRIBUTING.md](CONTRIBUTING.md) para detalhes.

## 📝 Changelog

### v1.0.0 (2026-02-22)
- ✨ Release inicial
- Backup filtrado por empresa(s) com `--companies`
- Import com remapeamento completo de IDs
- Dry-run para preview de importação
- Backup de segurança automático pré-import
- Transação atômica PostgreSQL
- Documentação completa

## 📄 Licença

MIT License — veja [LICENSE](LICENSE) para detalhes.

## ⚠️ Disclaimer

Este software é fornecido "como está", sem garantias. Sempre teste em ambiente não-produção primeiro. Mantenha backups regulares do seu banco de dados.

## 🔗 Links Úteis

- [Ticketz (Sistema original)](https://github.com/ticketz-oss/ticketz)
- [ticketz-sidekick (Projeto base)](https://github.com/ticketz-oss/ticketz-sidekick)
- [Auto-instalador Ticketz](https://ticke.tz)
