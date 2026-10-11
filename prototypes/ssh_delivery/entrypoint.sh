#!/bin/sh
set -eu
ssh-keygen -A
exec /usr/sbin/sshd -D -e -f /etc/ssh/sshd_config
