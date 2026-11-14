# PostgreSQL for the subscription database (local Docker only; Azure would use Azure Database for PostgreSQL).
FROM postgres:16

# Runs once, when the data volume is first initialised: read-only role for the shiperp-dashboard container.
COPY services/db/init/10-dashboard-readonly.sh /docker-entrypoint-initdb.d/10-dashboard-readonly.sh
# Executable = run as its own process (a non-executable .sh would be sourced into the entrypoint); strip CRLF
# in case the file was checked out on Windows.
RUN sed -i 's/\r$//' /docker-entrypoint-initdb.d/10-dashboard-readonly.sh \
    && chmod 755 /docker-entrypoint-initdb.d/10-dashboard-readonly.sh
