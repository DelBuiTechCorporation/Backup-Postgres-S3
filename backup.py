#!/usr/bin/env python3
import os
import subprocess
import tempfile
import boto3
from botocore.config import Config
from urllib.parse import urlparse
import sys
import re


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
            print(filtered_err, file=sys.stderr)
        raise RuntimeError('Falha ao listar bancos')
    out = filter_noise(proc.stdout)
    dbs = [l.strip() for l in out.splitlines() if l.strip()]
    return dbs


def dump_database(user, password, host, port, dbname, out_path):
    env = os.environ.copy()
    if password:
        env['PGPASSWORD'] = password
    cmd = [
        'pg_dump', '-h', host, '-p', str(port), '-U', user, '-F', 'c', '-f', out_path, dbname
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
            print(filtered_err, file=sys.stderr)
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
    parts = [p for p in meta.split('@') if p]
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
        # prefix@bucket@endpoint@forcepatch@access@secret@postgres://...
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
        # aplicar IGNORE_DATABASES (global) para pular bancos
        ignore_spec = os.environ.get('IGNORE_DATABASES', '')
        ignores = [s.strip() for s in ignore_spec.split(',') if s.strip()]
        if ignores:
            print(f'Ignorando bancos: {ignores}')
            dbs = [d for d in dbs if d not in ignores]
        # retenção: global RETENTION_DAYS ou meta 'retention'
        retention_global = os.environ.get('RETENTION_DAYS')
        retention = int(meta.get('retention')) if meta.get('retention') else (int(retention_global) if retention_global else None)
        # definir base_dir (dentro do prefix haverá pastas por db). Se prefix vazio, usa host como base
        base_dir = prefix.rstrip('/') if prefix else host
        for db in dbs:
            # timestamp components (timezone-aware UTC)
            now = __import__('datetime').datetime.now(__import__('datetime').timezone.utc)
            hour = now.hour
            minute = now.minute
            day = now.day
            month = now.month
            year = now.year
            # nome do arquivo solicitado: prefix-db-14h-01m-07d-09mês-2025y.dump
            if prefix:
                filename = f"{prefix}-{db}-{hour:02d}h-{minute:02d}m-{day:02d}d-{month:02d}mês-{year}y.dump"
            else:
                filename = f"{db}-{hour:02d}h-{minute:02d}m-{day:02d}d-{month:02d}mês-{year}y.dump"

            # dump temporário local
            with tempfile.NamedTemporaryFile(prefix=f'{db}-', suffix='.dump', delete=False) as tmpf:
                tmp_path = tmpf.name
            print(f'Fazendo dump de {db} para {tmp_path}...')
            dump_database(user, password, host, port, db, tmp_path)

            # escolhe bucket: db-specific > conn-specific > global
            bucket = db_buckets.get(db) or conn_bucket
            if not bucket:
                raise RuntimeError('Nenhum bucket configurado para upload (db, conn ou global)')

            # chave no S3: {base_dir}/{db}/{filename}
            key = f"{base_dir}/{db}/{filename}"
            print(f'Enviando para s3://{bucket}/{key}...')
            upload_file(s3, bucket, key, tmp_path)
            os.remove(tmp_path)
            print('Feito')
        # aplicar retenção: remover objetos mais antigos que retention dias (se configurado)
        if retention:
            try:
                print(f'Aplicando retenção de {retention} dias para bucket(s) desta conexão...')
                # listar objetos no bucket com prefix host-
                now = __import__('datetime').datetime.now(__import__('datetime').timezone.utc)
                cutoff = now - __import__('datetime').timedelta(days=retention)

                # usar o recurso para filtrar por prefix base_dir/db/
                s3_resource = boto3.resource('s3', aws_access_key_id=conn_s3.get('access'), aws_secret_access_key=conn_s3.get('secret'), region_name=conn_s3.get('region'), endpoint_url=conn_s3.get('endpoint'))
                bucket_name = conn_bucket
                if bucket_name:
                    bucket_obj = s3_resource.Bucket(bucket_name)
                    # para cada banco, filtrar por prefix = base_dir/db/
                    for db in dbs:
                        obj_prefix = f"{base_dir}/{db}/"
                        for obj in bucket_obj.objects.filter(Prefix=obj_prefix):
                            key = obj.key
                            if not key.endswith('.dump'):
                                continue
                            # extrair timestamp do nome: filename tem estrutura ...-HHh-MMm-DDd-Mmês-YYYYy.dump
                            try:
                                parts = key.rsplit('-', 5)
                                if len(parts) < 5:
                                    continue
                                # últimos 5 segmentos devem conter hour, minute, day, month, year with suffixes
                                hour_s, minute_s, day_s, month_s, year_s = parts[-5:]
                                def digits(s):
                                    return ''.join(ch for ch in s if ch.isdigit())
                                h = int(digits(hour_s))
                                m = int(digits(minute_s))
                                d = int(digits(day_s))
                                mo = int(digits(month_s))
                                y = int(digits(year_s))
                                obj_ts = __import__('datetime').datetime(y, mo, d, h, m, tzinfo=__import__('datetime').timezone.utc)
                            except Exception:
                                continue
                            if obj_ts < cutoff:
                                print(f'Apagando objeto antigo s3://{bucket_name}/{key} (ts={h:02d}-{m:02d}-{d:02d}-{mo:02d}-{y})')
                                s3.delete_object(Bucket=bucket_name, Key=key)
            except Exception as e:
                print('Falha ao aplicar retenção:', e)
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
        # opçao de terminar sessões: per-connection meta 'force_terminate' ou global env
        force_term_global = os.environ.get('FORCE_TERMINATE_AFTER_BACKUP', 'false')
        force_term = (str(meta.get('force_terminate') or force_term_global).lower() in ('1', 'true', 'yes'))
        if force_term:
            try:
                print(f'Forçando término de sessões do usuário {user} em {host}:{port}...')
                envp = os.environ.copy()
                if password:
                    envp['PGPASSWORD'] = password
                term_cmd = [
                    'psql', '-h', host, '-p', str(port), '-U', user, '-c',
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename = '" + user + "' AND pid <> pg_backend_pid();"
                ]
                proc = subprocess.run(term_cmd, env=envp, capture_output=True, text=True)
                if proc.returncode != 0:
                    print('Aviso: falha ao terminar sessões:', proc.stderr)
                else:
                    print('Sessões terminadas (se houver).')
            except Exception as e:
                print('Erro ao forçar término de sessões:', e)
