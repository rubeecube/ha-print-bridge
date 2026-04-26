# Standalone CUPS container with cups-pdf virtual printer.
# Does NOT use HA supervisor base images — works in plain Docker Compose.
#
# cups-pdf saves every print job as a PDF to:
#   /var/spool/cups-pdf/ANONYMOUS/<job-name>.pdf
#
# The validate.sh script mounts that directory to verify end-to-end delivery.

FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      cups \
      cups-pdf \
      cups-filters \
      printer-driver-all \
 && apt-get clean \
 && rm -rf /var/lib/apt/lists/*

# Allow CUPS to accept connections from any host (needed for Docker networking).
RUN sed -i \
      -e 's|^Listen localhost:631|Port 631|' \
      -e 's|^#ServerName.*|ServerName cups|' \
      /etc/cups/cupsd.conf \
 && printf '\n<Location />\n  Order allow,deny\n  Allow all\n</Location>\n\n<Location /admin>\n  Order allow,deny\n  Allow all\n</Location>\n' \
      >> /etc/cups/cupsd.conf

# cups-pdf output dir — must be world-writable so CUPS can write jobs there.
RUN mkdir -p /var/spool/cups-pdf/ANONYMOUS \
 && chmod 1777 /var/spool/cups-pdf /var/spool/cups-pdf/ANONYMOUS

COPY add-printer.sh /usr/local/bin/add-printer.sh
RUN chmod +x /usr/local/bin/add-printer.sh

EXPOSE 631

HEALTHCHECK --interval=5s --timeout=3s --retries=10 \
    CMD lpstat -h localhost -r 2>/dev/null || exit 1

CMD ["/usr/local/bin/add-printer.sh"]
