# Postgres backup para S3 (Minio compatível)

Esta imagem/serviço lista todos os bancos de dados de uma conexão Postgres e faz um `pg_dump` por banco, enviando os dumps para um bucket S3 compatível com Minio ou outro endpoint compatível com S3.

Como funciona (resumo):
- O serviço recebe uma lista `PG_URLS` com uma ou mais conexões Postgres. Cada conexão pode incluir metadados que definem o `prefix`, `bucket`, `endpoint`, `access`, `secret`, `region` e `force_path_style` apenas para aquela conexão.
- Para cada conexão, o serviço lista todos os bancos (exceto templates) e faz um `pg_dump -F c` por banco.
- Para cada dump, é escolhido o bucket na ordem: mapeamento por-banco (se definido) > bucket por-connection > bucket global (fallback). A chave S3 gerada é `[{GLOBAL_PREFIX}/]{host}-{db}.dump`.

Variáveis de ambiente principais e comportamento:
- `PG_URLS`: lista separada por vírgula de itens. Cada item pode conter metadados antes da URL do Postgres.
  - Sintaxe: `[meta@]postgres://user:pass@host:port/db`
  - Onde `meta` pode ser vários campos separados por `@`, por exemplo: `myprefix@bucket=backups1@endpoint=http://minio:9000@postgres://user:pass@host:5432/db`
  - Campos suportados em `meta`: `prefix`, `bucket`, `endpoint`, `access`, `secret`, `region`, `force_path_style`, `db_buckets`.
  - `db_buckets` permite mapear bancos específicos para buckets no formato `db1=bucket1,db2=bucket2` (colocado em `meta` como `db_buckets=db1=bucket1,db2=bucket2`).
- `S3_ENDPOINT`, `S3_ACCESS_KEY`, `S3_SECRET_KEY`, `S3_BUCKET`, `S3_REGION`: valores globais usados como fallback quando não existem por-connection.
- `S3_FORCE_PATH_STYLE` (true/false): define se o cliente S3 deve usar path-style addressing. Útil para Minio ou endpoints que não suportam virtual-host style.
- `GLOBAL_PREFIX`: prefixo opcional global para as chaves no bucket.

Exemplos práticos:

- Exemplo simples sem metadados (usa bucket global):
  - `PG_URLS=postgres://postgres:postgres@postgres:5432/postgres`

- Exemplo com metadados por conexão (prefix + bucket + endpoint):
  - `PG_URLS=myprefix@bucket=backups1@endpoint=http://minio:9000@postgres://postgres:postgres@postgres:5432/postgres`

- Exemplo com mapeamento por-banco (por-conn):
  - `PG_URLS=conn1@bucket=backups1@db_buckets=postgres=postgres-backups,other=other-backups@postgres://user:pass@host:5432/db`
  - Aqui o dump do banco `postgres` irá para o bucket `postgres-backups`, enquanto `other` irá para `other-backups`.

Docker Compose de exemplo (ajustar conforme necessário):

```yaml
version: '3.8'
services:
  minio:
    image: minio/minio:latest
    command: server /data
    environment:
      MINIO_ROOT_USER: minioadmin
      MINIO_ROOT_PASSWORD: minioadmin
    ports:
      - "9000:9000"
    volumes:
      - minio-data:/data

  pg-backup:
    build: .
    environment:
      PG_URLS: "myprefix@bucket=backups1@endpoint=http://minio:9000@postgres://postgres:postgres@postgres:5432/postgres"
      S3_ENDPOINT: "http://minio:9000"
      S3_ACCESS_KEY: "minioadmin"
      S3_SECRET_KEY: "minioadmin"
      S3_BUCKET: "backups"
      S3_FORCE_PATH_STYLE: "true"
    depends_on:
      - minio
      - postgres
    networks:
      - backup-net

  postgres:
    image: postgres:15-alpine
    environment:
      POSTGRES_PASSWORD: postgres
    ports:
      - "5432:5432"
    networks:
      - backup-net

volumes:
  minio-data:

networks:
  backup-net:
```

Sugestão de `.env.example` (copie para um arquivo `.env.example` local se necessário):

```env
# Exemplo .env.example
PG_URLS="myprefix@bucket=backups1@endpoint=http://minio:9000@postgres://postgres:postgres@postgres:5432/postgres,postgres://other:other@db:5432/main"

# Global S3 (fallback)
S3_ENDPOINT=http://minio:9000
S3_ACCESS_KEY=minioadmin
S3_SECRET_KEY=minioadmin
S3_BUCKET=backups
S3_REGION=us-east-1
S3_FORCE_PATH_STYLE=true

# Prefixo global (opcional)
GLOBAL_PREFIX=

# Logging
LOG_LEVEL=INFO
```

Observações e melhorias possíveis:
- Adicionar compressão dos dumps (ex.: gzip) antes do upload.
- Implementar retenção/rotação e notificações.
- Suporte a agendamento interno (cron) ou execução única via comando do container.
