FROM python:3.13-alpine

RUN apk add --no-cache openssh-server \
    && adduser -D -s /bin/sh nexo-import \
    && passwd -d nexo-import \
    && mkdir -p /run/sshd /run/consumed /etc/ssh/authorized_keys \
    && chown nexo-import:nexo-import /home/nexo-import \
    && chmod 700 /home/nexo-import \
    && chown nexo-import:nexo-import /run/consumed \
    && chmod 700 /run/consumed

COPY sshd_config /etc/ssh/sshd_config
COPY receiver.py /usr/local/bin/receive-credentials
COPY entrypoint.sh /usr/local/bin/ssh-poc-entrypoint
RUN chmod 755 /usr/local/bin/receive-credentials /usr/local/bin/ssh-poc-entrypoint

EXPOSE 2222
ENTRYPOINT ["/usr/local/bin/ssh-poc-entrypoint"]
