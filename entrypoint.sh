#!/usr/bin/env bash
set -euo pipefail

# configura timezone
TIMEZONE=${TIMEZONE:-America/Sao_Paulo}
if [ -f /usr/share/zoneinfo/${TIMEZONE} ]; then
  cp /usr/share/zoneinfo/${TIMEZONE} /etc/localtime
  echo "${TIMEZONE}" > /etc/timezone
fi

if [[ -z "${PG_URLS-}" ]]; then
  echo "É necessário definir PG_URLS"
  exit 1
fi

CRON_ENABLED=${CRON_ENABLED:-true}
CRON_SCHEDULE=${CRON_SCHEDULE:-"0 3 * * *"} # padrão: 03:00 diário

# normaliza aliases do tipo @daily, @hourly, @weekly, @monthly, @yearly
if [[ "${CRON_SCHEDULE}" == @* ]]; then
  case "${CRON_SCHEDULE}" in
    @hourly)
      CRON_SCHEDULE="0 * * * *"
      ;;
    @daily)
      CRON_SCHEDULE="0 0 * * *"
      ;;
    @weekly)
      CRON_SCHEDULE="0 0 * * 0"
      ;;
    @monthly)
      CRON_SCHEDULE="0 0 1 * *"
      ;;
    @yearly|@annually)
      CRON_SCHEDULE="0 0 1 1 *"
      ;;
    *)
      echo "Alias cron não reconhecido: ${CRON_SCHEDULE}, usando como está"
      ;;
  esac
fi

run_backup() {
  python /app/backup.py
}

if [ "${CRON_ENABLED}" = "true" ] || [ "${CRON_ENABLED}" = "1" ]; then
  echo "Agendando cron: ${CRON_SCHEDULE}"
  echo "${CRON_SCHEDULE} root /app/entrypoint.sh run" > /etc/crontabs/root
  crond -f -l 8 &
  # roda uma vez na inicialização também
  run_backup
  wait
else
  if [ "${1-}" = "run" ]; then
    run_backup
  else
    run_backup
  fi
fi
