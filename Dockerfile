# Ansible control node for uxplay-atom.
#
# Debian 12's ansible-core is 2.14; this pins something current and keeps the
# control machine clean. The image carries the whole control-side toolchain --
# ansible, ffmpeg for the test clips, iperf3 for the throughput probe -- so
# the only thing needed on the host is Docker.
FROM python:3.13-slim

ARG ANSIBLE_CORE_VERSION=2.19.11
# Match the invoking user so files written into the mounted repo (results/,
# generated clips) come back owned by you rather than by root.
ARG UID=1000
ARG GID=1000

ENV PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    ANSIBLE_FORCE_COLOR=1

RUN apt-get update && apt-get install --no-install-recommends -y \
        openssh-client \
        sshpass \
        rsync \
        ffmpeg \
        iperf3 \
        git \
        ca-certificates \
        less \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir \
        "ansible-core==${ANSIBLE_CORE_VERSION}" \
        ansible-lint

# The playbooks are ansible.builtin only, so no collections are installed.
# If that ever changes, add a requirements.yml and install it here.

RUN groupadd -g "${GID}" ansible 2>/dev/null || true \
    && useradd -m -u "${UID}" -g "${GID}" -s /bin/bash ansible

USER ansible
WORKDIR /work

# Ansible writes temp files under HOME; ~/.ssh arrives as a read-only mount.
ENV HOME=/home/ansible

ENTRYPOINT ["/bin/bash", "-lc"]
CMD ["ansible-playbook --syntax-check site.yml fetch-results.yml && echo OK"]
