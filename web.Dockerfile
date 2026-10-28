# Official Docker CLI image includes the Compose plugin. No Docker daemon runs inside Web.
FROM docker:27-cli
RUN apk add --no-cache python3 py3-yaml bash
ENV PYTHONUNBUFFERED=1
# Application and mutable config are mounted at the identical absolute host path.
# deploy.sh provides working_dir, command, and all host bindings through Compose.
