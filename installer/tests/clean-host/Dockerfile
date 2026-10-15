# A throwaway "fresh Ubuntu server" with systemd and no Docker, for testing install.sh.
FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update -qq \
 && apt-get install -y -qq systemd systemd-sysv curl ca-certificates iproute2 sudo procps >/dev/null \
 && rm -rf /var/lib/apt/lists/* \
 && useradd -m -s /bin/bash gamer \
 && echo 'gamer ALL=(ALL) NOPASSWD:ALL' >/etc/sudoers.d/gamer
STOPSIGNAL SIGRTMIN+3
CMD ["/sbin/init"]
