# pg-bkp — Backup de bancos Postgres para S3 (Minio compatível)

Projeto simples para automatizar dumps de bancos Postgres e enviá-los para um storage S3-compatível (ex.: Minio). O foco é ser leve e funcionar tanto em containers quanto em ambientes tradicionais.

## Visão geral

- O serviço lê uma ou mais conexões Postgres de `PG_URLS`, lista bancos não-template e gera um `pg_dump -F c` por banco.
- Cada dump é enviado para um bucket S3 (pode ser global, por-connection ou por-banco).
- Possui retenção configurável por dias calendariais (ex.: `RETENTION_DAYS=1` mantém apenas os dumps com a data do dia atual).

## Principais arquivos

- `backup.py`: script principal que realiza listagem de DBs, gera dumps e aplica retenção.
- `entrypoint.sh`: wrapper para execução (cron + inicialização imediata no container).
- `Dockerfile`: imagem docker para rodar o serviço.

## Variáveis de ambiente

As variáveis abaixo controlam comportamento do serviço. Você pode definir globalmente (para todas as conexões) ou por-connection usando metadados em `PG_URLS`.

- `PG_URLS` (obrigatório): lista separada por vírgula de conexões Postgres. Cada item pode ter metadados antes da URL.
  - Formatos suportados:
    - Meta-annotated: `prefix@bucket@endpoint@...@postgres://user:pass@host:port/db` (valores key=value também permitidos).
    - Posicional compacto: `prefix@bucket@endpoint@forcepath@access@secret@postgres://...`
  - Exemplo com metadados:
    `PG_URLS=myprefix@bucket=backups1@postgres://postgres:postgres@postgres:5432/postgres`

- `db_buckets` (opcional, por-connection): mapeamento `db1=bucket1,db2=bucket2` para direcionar backups de bancos específicos a buckets distintos.

- `S3_ENDPOINT`, `S3_ACCESS_KEY`, `S3_SECRET_KEY`, `S3_BUCKET`, `S3_REGION`: configurações globais de S3 (fallback se não informadas por connection).

- `S3_FORCE_PATH_STYLE` (true/false): força path-style addressing para S3/Minio.

- `GLOBAL_PREFIX`: prefixo opcional adicionado à chave de cada objeto no bucket.

- `RETENTION_DAYS` (inteiro): número de dias calendariais a manter. Exemplos:
  - `1`: mantém somente backups com data igual ao dia atual;
  - `7`: mantém backups dos últimos 7 dias (hoje e 6 dias anteriores);
  - `0` ou ausente: sem retenção automática.

- `CRON_ENABLED` (true/false): ativa execução via cron dentro do container. Padrão: `true`.
- `CRON_SCHEDULE`: expressão cron ou alias (`@daily`, `@hourly`, etc.). Padrão: `0 3 * * *`.
- `TIMEZONE`: fuso usado para timestamps (padrão `America/Sao_Paulo`).

- `IGNORE_DATABASES`: lista de bancos a ignorar (ex.: `postgres,template0`).

- `FORCE_TERMINATE_AFTER_BACKUP` (global) ou `force_terminate` (por-connection): `true|false`. Se `true`, o script tenta terminar sessões do usuário usado pelo backup após os uploads.

## Como rodar (exemplo com docker-compose)

1. Crie um arquivo `.env` com as variáveis necessárias (ex.: `PG_URLS`, `S3_*`, `RETENTION_DAYS`).
2. Ajuste `docker-compose.yml` para montar volumes se quiser logs persistentes.
3. Suba o serviço:

```bash
docker-compose up -d
```

O `entrypoint.sh` agendará o cron conforme `CRON_SCHEDULE` e também rodará uma execução imediata na inicialização.

## Formato de nomes e chaves S3

- Path no bucket: `{base_dir}/{db}/{filename}` onde `base_dir` é `prefix` (se configurado) ou o host do Postgres.
- `filename`: formato `(prefix-)?{db}-{HH}h-{MM}m-{DD}d-{MM}mês-{YYYY}y.dump`.

Exemplo de chave resultante:

```text
s3://mybucket/myprefix/mydb/myprefix-mydb-09h-09m-08d-09mês-2025y.dump
```

## Retenção (detalhes comportamentais)

- A retenção agora é feita por data calendarial: o script converte a data do timestamp extraído do nome do arquivo e compara com uma `cutoff_date` calculada a partir de `RETENTION_DAYS`.
- Com `RETENTION_DAYS=1`, permanecem apenas arquivos cuja data é a data atual. Arquivos do dia anterior serão apagados independentemente da diferença em horas.
- Dentro do período de retenção (ex.: últimos N dias), o script mantém apenas um backup por dia (o mais recente) e apaga duplicatas do mesmo dia.

## Logs e depuração

- Logs são enviados ao console e também gravados em `/var/log/pg-backup.log` (rotacionado).
- Defina `LOG_LEVEL=DEBUG` para obter mensagens mais verbosas, inclusive durante a rotina de retenção.

## Exemplos de uso

- Backup simples para bucket global:

```bash
PG_URLS=postgres://user:pass@db:5432/postgres \
S3_ENDPOINT=http://minio:9000 S3_ACCESS_KEY=minio S3_SECRET_KEY=minio123 S3_BUCKET=backups \
RETENTION_DAYS=7 docker-compose up -d
```

- Conexão com mapeamento por-banco:

```bash
PG_URLS=conn1@db_buckets=prod=prod-backups,other=other-backups@postgres://user:pass@host:5432/postgres
```

## Considerações de segurança

- Evite colocar credenciais sensíveis diretamente em `PG_URLS` quando possível. Use secrets do Docker ou seu orquestrador para injetar `S3_ACCESS_KEY` e `S3_SECRET_KEY`.
- Restrinja permissões do usuário de backup no Postgres — prefira um usuário com privilégios mínimos necessários.

## Contribuindo

- Abra uma issue para bugs ou propostas de melhoria.
- Pull requests são bem-vindos — por favor inclua testes quando possível.

## Licença

- Este projeto é distribuído sob a licença MIT. Veja o arquivo `LICENSE` no repositório.

- Copyright © 2025 Victor Delbui / DelBuiTechCorporation
