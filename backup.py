def s3_client_from_env():
def upload_file(s3, bucket, key, path):
#!/usr/bin/env python3
import os
import subprocess
import tempfile
import boto3
from botocore.config import Config
from urllib.parse import urlparse
import sys


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
    if proc.returncode != 0:
        print(proc.stderr, file=sys.stderr)
        raise RuntimeError('Falha ao listar bancos')
    dbs = [l.strip() for l in proc.stdout.splitlines() if l.strip()]
    return dbs


def dump_database(user, password, host, port, dbname, out_path):
    env = os.environ.copy()
    if password:
        env['PGPASSWORD'] = password
    cmd = [
        'pg_dump', '-h', host, '-p', str(port), '-U', user, '-F', 'c', '-f', out_path, dbname
    ]
    proc = subprocess.run(cmd, env=env, capture_output=True, text=True)
    if proc.returncode != 0:
        print(proc.stderr, file=sys.stderr)
        raise RuntimeError(f'Falha no pg_dump de {dbname}')


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
    for part in [p for p in meta.split('@') if p]:
        if '=' in part:
            k, v = part.split('=', 1)
            conn_meta[k.lower()] = v
        else:
            conn_meta['prefix'] = part
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
    pg_urls = os.environ.get('PG_URLS')
    if not pg_urls:
        print('Defina PG_URLS com uma ou mais conexões Postgres (separadas por ,)')
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
        print(f'Conectando em {host}:{port} como {user} para prefix "{prefix}"')
        dbs = list_databases(user, password, host, port)
        # retenção: global RETENTION_DAYS ou meta 'retention'
        retention_global = os.environ.get('RETENTION_DAYS')
        retention = int(meta.get('retention')) if meta.get('retention') else (int(retention_global) if retention_global else None)
        for db in dbs:
            with tempfile.NamedTemporaryFile(prefix=f'{db}-', suffix='.dump', delete=False) as tmpf:
                tmp_path = tmpf.name
            print(f'Fazendo dump de {db} para {tmp_path}...')
            dump_database(user, password, host, port, db, tmp_path)

            # escolhe bucket: db-specific > conn-specific > global
            bucket = db_buckets.get(db) or conn_bucket
            if not bucket:
                raise RuntimeError('Nenhum bucket configurado para upload (db, conn ou global)')

            key_prefix = prefix.rstrip('/') if prefix else ''
            key = f"{key_prefix}/{host}-{db}.dump" if key_prefix else f"{host}-{db}.dump"
            print(f'Enviando para s3://{bucket}/{key}...')
            upload_file(s3, bucket, key, tmp_path)
            os.remove(tmp_path)
            print('Feito')
        # nota: retenção será aplicada por mecanismo separado (ex: lifecycle no S3) ou pode ser implementada aqui
        if retention:
            print(f'Retention para conexão {conn_url} configurada: {retention} dias (observe que remoção automática não está implementada no cliente)')
