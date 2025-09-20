#!/usr/bin/env python3
import os
import subprocess
import tempfile
import boto3
from botocore.config import Config
from urllib.parse import urlparse
import sys
import re
import logging
from logging.handlers import RotatingFileHandler
import zipfile
import pyminizip

# logging setup
LOG_LEVEL = os.environ.get('LOG_LEVEL', 'INFO').upper()
try:
    CONSOLE_LEVEL = getattr(logging, LOG_LEVEL)
except Exception:
    CONSOLE_LEVEL = logging.INFO

logger = logging.getLogger('pg_backup')
logger.setLevel(logging.DEBUG)
log_path = os.environ.get('BACKUP_LOG_PATH', '/var/log/pg-backup.log')
try:
    fh = RotatingFileHandler(log_path, maxBytes=5 * 1024 * 1024, backupCount=3)
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
    logger.addHandler(fh)
except Exception:
    # if cannot create file handler, continue without it
    pass
ch = logging.StreamHandler()
ch.setLevel(logging.DEBUG)  # Sempre DEBUG para console, independente de LOG_LEVEL
ch.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
logger.addHandler(ch)
try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None


def parse_postgres_url(pg_url):
    parsed = urlparse(pg_url)
    user = parsed.username
    password = parsed.password
    host = parsed.hostname
    port = parsed.port or 5432
    return user, password, host, port


def list_databases(user, password, host, port):
    env = os.environ.copy()
    if password:
        env['PGPASSWORD'] = password
    cmd = [
        'psql', '-h', host, '-p', str(port), '-U', user, '-At', '-c',
        "SELECT datname FROM pg_database WHERE datistemplate = false;"
    ]
    proc = subprocess.run(cmd, env=env, capture_output=True, text=True)
    # filtrar linhas de noise do Postgres (ex.: TestJobs() database.c:...)
    def filter_noise(text):
        if not text:
            return ''
        noise_re = re.compile(r"TestJobs\(\)|database\.c:\d+")
        lines = [l for l in text.splitlines() if not noise_re.search(l)]
        return "\n".join(lines)

    if proc.returncode != 0:
        filtered_err = filter_noise(proc.stderr)
        if filtered_err:
            logger.error(filtered_err)
        raise RuntimeError('Falha ao listar bancos')
    out = filter_noise(proc.stdout)
    dbs = [l.strip() for l in out.splitlines() if l.strip()]
    return dbs


def dump_database(user, password, host, port, dbname, out_path):
    env = os.environ.copy()
    if password:
        env['PGPASSWORD'] = password
    cmd = [
        'pg_dump', '-h', host, '-p', str(port), '-U', user, '-F', 'p', '-f', out_path, dbname
    ]
    proc = subprocess.run(cmd, env=env, capture_output=True, text=True)
    # filtrar noise
    def filter_noise_local(text):
        if not text:
            return ''
        noise_re = re.compile(r"TestJobs\(\)|database\.c:\d+")
        lines = [l for l in text.splitlines() if not noise_re.search(l)]
        return "\n".join(lines)

    if proc.returncode != 0:
        filtered_err = filter_noise_local(proc.stderr)
        if filtered_err:
            logger.error(filtered_err)
        raise RuntimeError(f'Falha no pg_dump de {dbname}')


def zip_database(sql_path, zip_path, password=None):
    if password:
        # Usar pyminizip para compatibilidade com descompactadores padrão
        pyminizip.compress(sql_path, None, zip_path, password, 9)
        logger.info(f'Senha aplicada ao ZIP com pyminizip: {zip_path}')
    else:
        # Usar zipfile padrão para ZIPs sem senha
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
            zipf.write(sql_path, os.path.basename(sql_path))
        logger.info(f'ZIP sem senha: {zip_path}')
    os.remove(sql_path)


def build_s3_client_from_settings(settings):
    # settings: dict with keys endpoint, access, secret, region, force_path_style
    access = settings.get('access')
    secret = settings.get('secret')
    endpoint = settings.get('endpoint')
    region = settings.get('region') or os.environ.get('S3_REGION') or os.environ.get('AWS_REGION')
    force = settings.get('force_path_style')

    if not access or not secret:
        raise RuntimeError('S3 access/secret são obrigatórios (por-conn ou globais)')

    kwargs = dict(
        aws_access_key_id=access,
        aws_secret_access_key=secret,
        region_name=region,
    )
    if endpoint:
        kwargs['endpoint_url'] = endpoint

    s3_config = None
    if force is not None:
        addressing = 'path' if str(force).lower() in ('1', 'true', 'yes') else 'virtual'
        s3_config = Config(s3={'addressing_style': addressing})

    if s3_config:
        return boto3.client('s3', config=s3_config, **kwargs)
    return boto3.client('s3', **kwargs)


def upload_file(s3, bucket, key, path):
    s3.upload_file(path, bucket, key)


def parse_conn_item(item):
    item = item.strip()
    # localizar o inicio da URL do Postgres
    idx = item.find('postgres://')
    if idx == -1:
        idx = item.find('postgresql://')
    if idx == -1:
        raise RuntimeError('Item PG_URLS inválido, não contém postgres://')
    meta = item[:idx]
    url = item[idx:]
    conn_meta = {}
    parts = [p for p in meta.split('|') if p]
    # Se existir ao menos um par key=value, use parsing por chave
    if any('=' in p for p in parts):
        for part in parts:
            if '=' in part:
                k, v = part.split('=', 1)
                conn_meta[k.lower()] = v
            else:
                # if plain token found, treat as prefix if prefix not set
                if 'prefix' not in conn_meta:
                    conn_meta['prefix'] = part
    else:
        # Suporte ao formato posicional solicitado:
        # prefix|bucket|endpoint|forcepatch|access|secret|postgres://...
        # forcepatch (opcional) é 'true' ou 'false' e, se presente, vem logo após o endpoint
        if len(parts) >= 1:
            conn_meta['prefix'] = parts[0]
        if len(parts) >= 2:
            # remover possível sintaxe bucket(name) -> extrair conteúdo antes de '(' se existir
            b = parts[1]
            if '(' in b:
                b = b.split('(', 1)[0]
            conn_meta['bucket'] = b
        if len(parts) >= 3:
            conn_meta['endpoint'] = parts[2]
        # Verifica se há um campo force_path_style posicional (true/false) na posição 3
        idx = 3
        if len(parts) > idx and str(parts[idx]).lower() in ('true', 'false', '1', '0', 'yes', 'no'):
            conn_meta['force_path_style'] = parts[idx]
            idx += 1
        # O próximo(s) campos são access e secret (se existirem)
        if len(parts) > idx:
            conn_meta['access'] = parts[idx]
            idx += 1
        if len(parts) > idx:
            conn_meta['secret'] = parts[idx]

    return url, conn_meta


def parse_db_buckets(spec):
    # spec: 'db1=bucket1,db2=b2'
    mapping = {}
    if not spec:
        return mapping
    for pair in spec.split(','):
        if '=' in pair:
            db, b = pair.split('=', 1)
            mapping[db.strip()] = b.strip()
    return mapping


if __name__ == '__main__':
    logger.info('Iniciando processo de backup do PostgreSQL')
    pg_urls = os.environ.get('PG_URLS')
    if not pg_urls:
        logger.error('Defina PG_URLS com uma ou mais conexões Postgres (separadas por ,)')
        sys.exit(1)

    # global S3 fallback
    GLOBAL_S3 = {
        'endpoint': os.environ.get('S3_ENDPOINT'),
        'access': os.environ.get('S3_ACCESS_KEY'),
        'secret': os.environ.get('S3_SECRET_KEY'),
        'region': os.environ.get('S3_REGION') or os.environ.get('AWS_REGION'),
        'force_path_style': os.environ.get('S3_FORCE_PATH_STYLE'),
        'bucket': os.environ.get('S3_BUCKET')
    }

    # parse items
    items = [p.strip() for p in pg_urls.split(',') if p.strip()]
    for item in items:
        conn_url, meta = parse_conn_item(item)
        # build per-conn s3 settings by overriding globals with meta if present
        conn_s3 = {
            'endpoint': meta.get('endpoint') or GLOBAL_S3.get('endpoint'),
            'access': meta.get('access') or GLOBAL_S3.get('access'),
            'secret': meta.get('secret') or GLOBAL_S3.get('secret'),
            'region': meta.get('region') or GLOBAL_S3.get('region'),
            'force_path_style': meta.get('force_path_style') if meta.get('force_path_style') is not None else GLOBAL_S3.get('force_path_style')
        }

        db_buckets = parse_db_buckets(meta.get('db_buckets', ''))
        conn_bucket = meta.get('bucket') or GLOBAL_S3.get('bucket')
        prefix = meta.get('prefix', '') or os.environ.get('GLOBAL_PREFIX', '')

        s3 = build_s3_client_from_settings(conn_s3)

        user, password, host, port = parse_postgres_url(conn_url)
        logger.info(f'Conectando em {host}:{port} como {user} para prefix "{prefix}"')
        dbs = list_databases(user, password, host, port)
        # aplicar IGNORE_DATABASES (global) para pular bancos
        ignore_spec = os.environ.get('IGNORE_DATABASES', '')
        ignores = [s.strip() for s in ignore_spec.split(',') if s.strip()]
        if ignores:
            logger.info(f'Ignorando bancos: {ignores}')
            dbs = [d for d in dbs if d not in ignores]
        # retenção: global RETENTION_DAYS ou meta 'retention'
        retention_global = os.environ.get('RETENTION_DAYS')
        retention = int(meta.get('retention')) if meta.get('retention') else (int(retention_global) if retention_global else None)
        # definir base_dir (dentro do prefix haverá pastas por db). Se prefix vazio, usa host como base
        base_dir = prefix.rstrip('/') if prefix else host
        # timezone da aplicação
        tz_name = os.environ.get('TIMEZONE', 'America/Sao_Paulo')
        if ZoneInfo:
            try:
                tz = ZoneInfo(tz_name)
            except Exception:
                tz = __import__('datetime').timezone.utc
        else:
            tz = __import__('datetime').timezone.utc
        for db in dbs:
            # timestamp components (app timezone)
            now = __import__('datetime').datetime.now(tz)
            hour = now.hour
            minute = now.minute
            day = now.day
            month = now.month
            year = now.year
            # nome do arquivo solicitado: prefix-db-14h-01m-07d-09mes-2025y.zip
            if prefix:
                filename = f"{prefix}-{db}-{hour:02d}h-{minute:02d}m-{day:02d}d-{month:02d}mes-{year}y.zip"
            else:
                filename = f"{db}-{hour:02d}h-{minute:02d}m-{day:02d}d-{month:02d}mes-{year}y.zip"

            # dump temporário local
            with tempfile.NamedTemporaryFile(prefix=f'{db}-', suffix='.sql', delete=False) as tmpf:
                tmp_path = tmpf.name
            print(f'Fazendo dump de {db} para {tmp_path}...')
            dump_database(user, password, host, port, db, tmp_path)

            # zipar o arquivo SQL
            zip_password = os.environ.get('ZIP_PASSWORD')
            if zip_password:
                logger.info(f'ZIP_PASSWORD definido: {len(zip_password)} caracteres')
            else:
                logger.info('ZIP_PASSWORD não definido')
            zip_path = tmp_path.replace('.sql', '.zip')
            logger.info(f'Zipando {tmp_path} para {zip_path}...')
            zip_database(tmp_path, zip_path, zip_password)

            # usar o zip_path para upload
            upload_path = zip_path

            # escolhe bucket: db-specific > conn-specific > global
            bucket = db_buckets.get(db) or conn_bucket
            if not bucket:
                raise RuntimeError('Nenhum bucket configurado para upload (db, conn ou global)')

            # chave no S3: {base_dir}/{db}/{filename}
            key = f"{base_dir}/{db}/{filename}"
            logger.info(f'Enviando para s3://{bucket}/{key}...')
            upload_file(s3, bucket, key, upload_path)
            os.remove(upload_path)
            logger.info('Backup concluído com sucesso')
        # aplicar retenção: remover objetos mais antigos que retention dias (se configurado)
        if retention:
            try:
                logger.info(f'Aplicando retenção de {retention} dias para bucket(s) desta conexão...')
                # listar objetos no bucket com prefix host- usando mesma timezone
                now = __import__('datetime').datetime.now(tz)
                # retenção por dias calendariais: calcula a menor data a ser mantida
                # Ex.: retention=1 -> manter apenas objetos com data == today
                #       retention=7 -> manter objetos dos últimos 7 dias (incluindo hoje)
                from datetime import timedelta as _td
                if retention and int(retention) > 0:
                    cutoff_date = now.date() - _td(days=int(retention) - 1)
                else:
                    cutoff_date = now.date()

                # usar o recurso para filtrar por prefix base_dir/db/ e respeitar buckets por-db
                s3_resource = boto3.resource('s3', aws_access_key_id=conn_s3.get('access'), aws_secret_access_key=conn_s3.get('secret'), region_name=conn_s3.get('region'), endpoint_url=conn_s3.get('endpoint'))
                # iterar por banco e escolher bucket específico se houver
                for db in dbs:
                    bucket_name = db_buckets.get(db) or conn_bucket
                    if not bucket_name:
                        # nada a fazer para este banco se não houver bucket configurado
                        continue
                    bucket_obj = s3_resource.Bucket(bucket_name)
                    obj_prefix = f"{base_dir}/{db}/"
                    # coletar objetos com timestamp parseado
                    objs = []
                    for obj in bucket_obj.objects.filter(Prefix=obj_prefix):
                        key = obj.key
                        filename = key.split('/')[-1]
                        if not filename.endswith('.zip'):
                            continue
                        try:
                            name = filename[:-4]  # remover .zip
                            parts = name.split('-')
                            if len(parts) < 6:
                                continue
                            hour_s, minute_s, day_s, month_s, year_s = parts[-5:]
                            def digits(s):
                                return ''.join(ch for ch in s if ch.isdigit())
                            h = int(digits(hour_s))
                            m = int(digits(minute_s))
                            d = int(digits(day_s))
                            mo = int(digits(month_s))
                            y = int(digits(year_s))
                            obj_ts = __import__('datetime').datetime(y, mo, d, h, m, tzinfo=tz)
                            objs.append((key, obj_ts))
                        except Exception:
                            continue

                    # primeiro: remover objetos estritamente mais antigos que cutoff
                    to_keep = []
                    for key, obj_ts in objs:
                        try:
                            obj_date = obj_ts.date()
                        except Exception:
                            # se falhar em extrair a data, pular
                            continue
                        if obj_date < cutoff_date:
                            logger.info(f'Apagando objeto antigo s3://{bucket_name}/{key} (ts={obj_ts.isoformat()})')
                            s3.delete_object(Bucket=bucket_name, Key=key)
                        else:
                            to_keep.append((key, obj_ts))

                    # agora: dentro do período retenção, garantir apenas 1 por dia (manter o mais recente por dia)
                    by_day = {}
                    for key, obj_ts in to_keep:
                        day_key = (obj_ts.year, obj_ts.month, obj_ts.day)
                        prev = by_day.get(day_key)
                        if not prev or obj_ts > prev[1]:
                            by_day[day_key] = (key, obj_ts)

                    # delete any duplicates (objects in to_keep not equal to the chosen one per day)
                    chosen = set(k for k, _ in by_day.values())
                    for key, obj_ts in to_keep:
                        if key not in chosen:
                            logger.info(f'Apagando objeto duplicado do dia s3://{bucket_name}/{key} (ts={obj_ts.isoformat()})')
                            s3.delete_object(Bucket=bucket_name, Key=key)
            except Exception as e:
                logger.error(f'Falha ao aplicar retenção: {e}')
            finally:
                # fechar cliente resource se foi criado
                try:
                    if 's3_resource' in locals() and getattr(s3_resource, 'meta', None):
                        s3_resource.meta.client.close()
                except Exception:
                    pass
        # cleanup do cliente s3 e variáveis sensíveis
        try:
            if hasattr(s3, 'close'):
                s3.close()
        except Exception:
            pass
        try:
            # remover PGPASSWORD caso tenha sido exportado globalmente por engano
            if 'PGPASSWORD' in os.environ:
                del os.environ['PGPASSWORD']
        except Exception:
            pass
        logger.info(f'Conexão para {host}:{port} processada com sucesso')
        # opçao de terminar sessões: per-connection meta 'force_terminate' ou global env
        force_term_global = os.environ.get('FORCE_TERMINATE_AFTER_BACKUP', 'false')
        force_term = (str(meta.get('force_terminate') or force_term_global).lower() in ('1', 'true', 'yes'))
        if force_term:
            try:
                logger.info(f'Forçando término de sessões do usuário {user} em {host}:{port}...')
                envp = os.environ.copy()
                if password:
                    envp['PGPASSWORD'] = password
                term_cmd = [
                    'psql', '-h', host, '-p', str(port), '-U', user, '-c',
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename = '" + user + "' AND pid <> pg_backend_pid();"
                ]
                proc = subprocess.run(term_cmd, env=envp, capture_output=True, text=True)
                if proc.returncode != 0:
                    logger.warning(f'Aviso: falha ao terminar sessões: {proc.stderr}')
                else:
                    logger.info('Sessões terminadas (se houver).')
            except Exception as e:
                logger.error(f'Erro ao forçar término de sessões: {e}')
