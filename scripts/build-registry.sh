#!/usr/bin/env bash
set -euo pipefail

REPO="${REPO:-matheus-meneses/aide-plugins}"
ASSET_BASE="${ASSET_BASE:-https://github.com/${REPO}/releases/latest/download}"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PLUGINS_DIR="${ROOT}/plugins"
DIST_DIR="${ROOT}/dist"
INDEX_OUT="${ROOT}/registry/index.yaml"

sha256() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | awk '{print $1}'
  else
    shasum -a 256 "$1" | awk '{print $1}'
  fi
}

field() {
  awk -v key="$1" '$1 == key":" { sub("^[^:]*:[[:space:]]*", ""); gsub(/^"|"$/, ""); print; exit }' "$2"
}

rm -rf "${DIST_DIR}"
mkdir -p "${DIST_DIR}"

{
  echo "plugins:"
} >"${INDEX_OUT}.tmp"

for plugin_dir in "${PLUGINS_DIR}"/*/; do
  [ -f "${plugin_dir}plugin.yaml" ] || continue
  manifest="${plugin_dir}plugin.yaml"

  name="$(field name "${manifest}")"
  version="$(field version "${manifest}")"
  runtime="$(field runtime "${manifest}")"

  if [ -z "${name}" ] || [ -z "${version}" ] || [ -z "${runtime}" ]; then
    echo "skip: ${plugin_dir} missing name/version/runtime" >&2
    continue
  fi

  tarball="${name}-${version}.tar.gz"
  manifest_asset="${name}-${version}.plugin.yaml"

  echo "packaging ${name}@${version} (${runtime})" >&2
  ( cd "${plugin_dir}" && shopt -s nullglob && \
    tar -czf "${DIST_DIR}/${tarball}" \
      --exclude='.venv' --exclude='__pycache__' --exclude='*.pyc' \
      -- * )
  cp "${manifest}" "${DIST_DIR}/${manifest_asset}"

  digest="$(sha256 "${DIST_DIR}/${tarball}")"

  {
    echo "  ${name}:"
    echo "    latest: ${version}"
    echo "    versions:"
    echo "      - version: ${version}"
    echo "        manifest_url: \"${ASSET_BASE}/${manifest_asset}\""
    echo "        artifacts:"
    echo "          ${runtime}:"
    echo "            url: \"${ASSET_BASE}/${tarball}\""
    echo "            sha256: \"${digest}\""
  } >>"${INDEX_OUT}.tmp"
done

mv "${INDEX_OUT}.tmp" "${INDEX_OUT}"
cp "${INDEX_OUT}" "${DIST_DIR}/index.yaml"

echo "wrote ${INDEX_OUT}" >&2
echo "artifacts in ${DIST_DIR}" >&2
