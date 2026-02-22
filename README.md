# 🔧 ticketz-sidekick2

Ferramenta avançada de backup, restore e importação de empresas para o [Ticketz](https://github.com/ticketz-oss/ticketz) (sistema multi-tenant de atendimento via WhatsApp).

## 📋 Sobre

Baseado no [ticketz-sidekick](https://github.com/ticketz-oss/ticketz-sidekick) original, o sidekick2 adiciona recursos para gerenciar backups filtrados por empresa e importar empresas entre instalações Ticketz diferentes.

- ✅ Backup filtrado por empresa(s) específica(s) com `--companies`
- ✅ Importação de empresa para instalação existente com remapeamento completo de IDs
- ✅ Backups salvos na **própria pasta do sidekick2** — independente da instalação Ticketz
- ✅ Company 1 (admin/sistema) sempre incluída automaticamente
- ✅ Conexões WhatsApp da company 1 excluídas para evitar conflitos de sessão
- ✅ Mídias filtradas para incluir apenas arquivos referenciados
- ✅ Roda como container **separado** ao lado da instalação Ticketz existente

## 🏗️ Arquitetura

```
~/
  ticketz-docker-acme/        instalação Ticketz (auto-instalador)
  sidekick2/                  este projeto (pasta separada)
    backups/                  backups gerados ficam AQUI
      ticketz-backup-xxx.tar.gz
```

```

  PostgreSQL (Container Ticketz)                         
   89 tabelas  multi-tenant por companyId             

                         

  sidekick2 (Container separado)                         
   sidekick2.sh        (orquestra backup/restore/import)
   ticketz-filter.py   (filtra dump por empresa)       
   ticketz-import.py   (remapeia IDs para importação)  

                                         
     [backup]         [restore]        [import]
    pg_dump           tar.gz         backup 
    filter           psql load       scan IDs 
    tar.gz            volumes         remap IDs 
     sidekick2/                      transação SQL
      backups/
```

## 📦 Estrutura do Projeto

```
sidekick2/
 sidekick2.sh            # Script principal (backup/restore/import)
 ticketz-filter.py       # Filtro de dump SQL por empresa (2 passagens)
 ticketz-import.py       # Importação com remapeamento de IDs
 Dockerfile              # Container baseado em postgres:16-alpine
 docker-compose.yaml     # Configuração standalone com rede/volumes externos
 README.md               # Esta documentação
 CONTRIBUTING.md         # Guia de contribuição
 LICENSE                 # Licença MIT
 backups/                # Backups gerados (gitignored)
```

## 🚀 Instalação

### Pré-requisitos

- Linux com Docker e Docker Compose
- Ticketz instalado (auto-instalador ou manual)
- Acesso ao servidor via SSH

### Setup

```bash
# Clone na pasta ~/sidekick2 (nome curto, separado do Ticketz)
git clone https://github.com/leostronggg/ticketz-sidekick2.git ~/sidekick2
cd ~/sidekick2
mkdir -p backups
docker compose build
```

> **Nota:** Pode usar qualquer nome de pasta. O que importa é que o sidekick2 esteja na mesma máquina que o Ticketz para acessar a rede e volumes Docker.

### Ajuste o prefixo da rede/volumes (se necessário)

O `docker-compose.yaml` usa `ticketz-docker-acme_` como prefixo  padrão do auto-instalador.
Se a pasta do Ticketz tem outro nome, ajuste o prefixo no `docker-compose.yaml`:

```bash
# Verificar o nome correto
docker network ls | grep ticketz
docker volume ls | grep ticketz
```

## 💡 Uso

### 1️⃣ Backup por Empresa (uso principal)

Gera um backup contendo apenas os dados de uma ou mais empresas específicas.
O arquivo `.tar.gz` é salvo em `~/sidekick2/backups/`.

```bash
cd ~/sidekick2

# Backup apenas da empresa 263 (company 1/admin sempre incluída)
docker compose run --rm sidekick2 backup --companies 263

# Múltiplas empresas
docker compose run --rm sidekick2 backup --companies 263,10,45

# Range de empresas
docker compose run --rm sidekick2 backup --companies 10-50

# Misto
docker compose run --rm sidekick2 backup --companies 263,10-20,45

# Apenas banco de dados (sem mídias — mais rápido)
docker compose run --rm sidekick2 backup --companies 263 --dbonly
```

O backup gerado fica em `~/sidekick2/backups/ticketz-backup-YYYYMMDDHHMMSS.tar.gz`

### 2️⃣ Backup Completo

```bash
cd ~/sidekick2

# Backup de todas as empresas
docker compose run --rm sidekick2 backup

# Apenas banco de dados
docker compose run --rm sidekick2 backup --dbonly
```

### 3️⃣ Restore

Restaura o backup mais recente de `~/sidekick2/backups/` para o banco e volumes do Ticketz.
**O banco deve estar vazio**  use após uma instalação limpa do Ticketz.

```bash
cd ~/sidekick2

# Coloque o arquivo .tar.gz em ~/sidekick2/backups/
# O restore usa o arquivo mais recente automaticamente
docker compose run --rm sidekick2 restore
```

### 4️⃣ Import (adicionar empresa a instalação existente)

Importa uma empresa de um backup filtrado para um banco Ticketz ativo.
Todos os IDs são remapeados para evitar conflitos. A operação é envolvida
em uma transação PostgreSQL  se qualquer coisa falhar, o banco **não é modificado**.

```bash
cd ~/sidekick2

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

## 🔄 Fluxos Completos

### Migrar empresa para novo servidor (instalação limpa)

**Servidor origem:**
```bash
cd ~/sidekick2
docker compose run --rm sidekick2 backup --companies 263
# Arquivo gerado: ~/sidekick2/backups/ticketz-backup-YYYYMMDDHHMMSS.tar.gz
# Baixe via SCP/FTP para sua máquina local
```

**Servidor destino — resetar e restaurar:**
```bash
# 1. Remover instalação antiga completamente
cd ~/ticketz-docker-acme
docker compose down -v          # remove containers + volumes Docker
cd ~
rm -rf ~/ticketz-docker-acme    # remove a pasta (backups estão em ~/sidekick2, não aqui)

# 2. Instalar Ticketz limpo
curl -sSL get.ticke.tz | sudo bash -s seudominio seuemail

# 3. sidekick2 já está em ~/sidekick2 — envie o backup para lá via SCP/FTP
#    (se ainda não instalou o sidekick2 neste servidor, veja Instalação acima)

# 4. Restaurar
cd ~/sidekick2
docker compose run --rm sidekick2 restore
```

### Atualizar empresa (re-restaurar com dados mais recentes)

```bash
# Servidor origem: gerar backup atualizado
cd ~/sidekick2
docker compose run --rm sidekick2 backup --companies 263

# Servidor destino: limpar banco e restaurar
cd ~/ticketz-docker-acme
docker compose down -v    # apaga os volumes (banco + mídias)
docker compose up -d      # recria tudo vazio, sobe containers

cd ~/sidekick2
# (coloque o novo .tar.gz em ~/sidekick2/backups/)
docker compose run --rm sidekick2 restore
```

## 🔄 Como Funciona o Filtro por Empresa

1. `pg_dump` gera o dump completo do banco
2. `ticketz-filter.py` executa um filtro em duas passagens:
   - **Passagem 1**: Lê o dump coletando IDs (contatos, tickets, usuários, etc.) das empresas selecionadas
   - **Passagem 2**: Escreve um novo dump mantendo apenas linhas pertencentes às empresas
3. Mídias em `/backend-public` e `/backend-private` são filtradas para manter apenas arquivos referenciados
4. O dump filtrado e mídias são empacotados em `ticketz-backup-*.tar.gz`

### Company 1 (Admin/Sistema)

A company 1 é a empresa de sistema do Ticketz. É **sempre incluída** porque:
- `GetSuperSettingService` é hardcoded para usar `companyId: 1`
- `CheckCompanyCompliant` trata company 1 como sempre em conformidade
- Seeds padrão criam a company 1 com configurações essenciais

As **conexões WhatsApp da company 1 são excluídas** para evitar conflitos de sessão ao restaurar em outro servidor.

## 📊 Classificação das Tabelas

| Categoria | Tabelas | Método de Filtro |
|---|---|---|
| **Globais** | Plans, Helps, Translations, SequelizeMeta, SequelizeData | Sem filtro (mantém tudo) |
| **Diretas** | Contacts, Tickets, Messages, Users, Queues, Whatsapps, +16 | Filtro por `companyId` |
| **Indiretas** | Baileys, BaileysKeys, ChatMessages, ContactTags, TicketTags, +16 | Filtro por FK  IDs coletados |
| **Empresa** | Companies | Filtro por `id` |

## ⚠️ Avisos Importantes

- **Backups ficam em `~/sidekick2/backups/`**  pasta do sidekick2, nunca dentro do Ticketz. Isso garante que ao deletar a pasta do Ticketz para reinstalar, os backups não são perdidos.
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

## 🔧 Troubleshooting

### Erro de rede/volume não encontrado

```bash
docker network ls | grep ticketz
docker volume ls | grep ticketz
# O prefixo deve corresponder ao docker-compose.yaml
```

### Erro de permissão no backup

```bash
mkdir -p ~/sidekick2/backups
chmod 755 ~/sidekick2/backups
```

### Verificar containers do Ticketz

```bash
docker ps | grep ticketz
```

## 🤝 Contribuindo

Contribuições são bem-vindas! Veja [CONTRIBUTING.md](CONTRIBUTING.md) para detalhes.

## 📝 Changelog

### v1.1.0 (2026-02-22)
-  Backups salvos na pasta do sidekick2 (independente do Ticketz)
-  README corrigido com fluxos completos de migração e restore
-  Clone com nome personalizado documentado (`~/sidekick2`)

### v1.0.0 (2026-02-22)
-  Release inicial
- Backup filtrado por empresa(s) com `--companies`
- Import com remapeamento completo de IDs
- Dry-run para preview de importação
- Backup de segurança automático pré-import
- Transação atômica PostgreSQL

## 📄 Licença

MIT License  veja [LICENSE](LICENSE) para detalhes.

## ⚠️ Disclaimer

Este software é fornecido "como está", sem garantias. Sempre teste em ambiente não-produção primeiro. Mantenha backups regulares do seu banco de dados.

## 🔗 Links Úteis

- [Ticketz (Sistema original)](https://github.com/ticketz-oss/ticketz)
- [ticketz-sidekick (Projeto base)](https://github.com/ticketz-oss/ticketz-sidekick)
- [Auto-instalador Ticketz](https://ticke.tz)
