# Postgres backup para S3 (Minio compatível)

Esta imagem/serviço lista os bancos de uma(s) instância(s) Postgres e gera um `pg_dump -F c` por banco, enviando para um S3 compatível (ex.: Minio).

Como funciona (resumo):

- Recebe `PG_URLS` com uma ou mais conexões; cada conexão pode ter metadados (prefix, bucket, endpoint, access, secret, region, force_path_style).
- Para cada conexão, lista bancos (exceto templates) e gera um dump por banco.
- Bucket escolhido: mapeamento por-banco > bucket por-connection > bucket global. Chave gerada: `[{GLOBAL_PREFIX}/]{host}-{db}.dump`.

Variáveis de ambiente principais:

- `PG_URLS`: itens separados por vírgula. Cada item pode incluir metadados antes da URL do Postgres.
  - Sintaxe: `[meta@]postgres://user:pass@host:port/db` ou formato posicional compacto:
    `prefix@bucket@endpoint@forcepatch@access@secret@postgres://...`
  - `db_buckets` permite mapear bancos específicos para buckets: `db1=bucket1,db2=bucket2`.
- `S3_ENDPOINT`, `S3_ACCESS_KEY`, `S3_SECRET_KEY`, `S3_BUCKET`, `S3_REGION`: fallback global.
- `S3_FORCE_PATH_STYLE` (true/false): global fallback para path-style addressing.
`GLOBAL_PREFIX`: prefixo opcional para chaves no bucket.

`RETENTION_DAYS`: retenção global em dias (ex.: 7). Pode ser sobrescrito por metadado `retention` por-connection.

`CRON_ENABLED` (true/false), `CRON_SCHEDULE` (cron string) e `TIMEZONE` (padrão: `America/Sao_Paulo`) controlam execução agendada.

`IGNORE_DATABASES`: lista separada por vírgula de nomes de bancos a serem ignorados (ex.: `postgres,template0`).

`CRON_SCHEDULE` aceita aliases: `@hourly`, `@daily`, `@weekly`, `@monthly`, `@yearly` (ou `@annually`). O `entrypoint.sh` converte esses aliases para uma expressão cron antes de gravar no crontab.
 - `FORCE_TERMINATE_AFTER_BACKUP`: `true|false` (global) ou metadado por-connection `force_terminate=true|false`. Quando ativo, o script executa `pg_terminate_backend` para o usuário do backup após terminar os uploads.

Estrutura e nomes dos arquivos:

- Os dumps são salvos no S3 em pastas por banco dentro do prefix: `{prefix_or_host}/{db}/{filename}`.
- Nome do arquivo: se `prefix` estiver configurado -> `(Prefix)-(Nome do Banco)-HH-MM-DD-Mes-Ano.dump`; caso contrário `db-HH-MM-DD-Mes-Ano.dump`.

Exemplo de chave S3 resultante:

`s3://mybucket/myprefix/mydb/myprefix-mydb-15-30-05-09-2025.dump`

Observações sobre `FORCE_TERMINATE_AFTER_BACKUP`:

- Use com cautela — termina sessões do mesmo usuário (exceto a atual) e pode interromper aplicações que usem esse usuário.
- Recomendo usar um usuário dedicado só para backups ou validar com `dry-run` antes de habilitar.

Exemplos rápidos:

- Simples (usa bucket global):
  `PG_URLS=postgres://postgres:postgres@postgres:5432/postgres`

- Metadados por conexão:
  `PG_URLS=myprefix@bucket=backups1@endpoint=http://minio:9000@postgres://postgres:postgres@postgres:5432/postgres`

- Formato posicional com `forcepatch`:
  `PG_URLS=myprefix@backups1@http://minio:9000@true@minio1key@minio1secret@postgres://postgres:postgres@postgres:5432/postgres`

- Mapeamento por-banco:
  `PG_URLS=conn1@bucket=backups1@db_buckets=postgres=postgres-backups,other=other-backups@postgres://user:pass@host:5432/db`

Docker Compose de exemplo (ajuste conforme necessário) está em `docker-compose.yml`.

Arquivo de exemplo de variáveis: `env.example` (no repositório). Copie para `.env` e ajuste seus valores.

Observações e melhorias possíveis:

- Adicionar compressão (gzip) antes do upload.
- Implementar remoção automática de objetos S3 mais antigos conforme `RETENTION_DAYS` (posso implementar se desejar).
- Oferecer suporte a Docker secrets ou variáveis de ambiente específicas por-prefixo para evitar colocar chaves em `PG_URLS`.
