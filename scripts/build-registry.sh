#!/usr/bin/env bash
set -euo pipefail

REPO="${REPO:-matheus-meneses/aide-plugins}"
ASSET_BASE="${ASSET_BASE:-https://github.com/${REPO}/releases/latest/download}"

GO_PLATFORMS="${GO_PLATFORMS:-darwin_amd64 darwin_arm64 linux_amd64 linux_arm64 windows_amd64}"

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
  description="$(field description "${manifest}")"
  icon="$(field icon "${manifest}")"

  if [ -z "${name}" ] || [ -z "${version}" ] || [ -z "${runtime}" ]; then
    echo "skip: ${plugin_dir} missing name/version/runtime" >&2
    continue
  fi

  manifest_asset="${name}-${version}.plugin.yaml"
  cp "${manifest}" "${DIST_DIR}/${manifest_asset}"

  {
    echo "  ${name}:"
    echo "    latest: ${version}"
    if [ -n "${description}" ]; then
      echo "    description: \"${description}\""
    fi
    if [ -n "${icon}" ]; then
      echo "    icon: \"${icon}\""
    fi
    echo "    versions:"
    echo "      - version: ${version}"
    echo "        manifest_url: \"${ASSET_BASE}/${manifest_asset}\""
    echo "        artifacts:"
  } >>"${INDEX_OUT}.tmp"

  case "${runtime}" in
    python)
      tarball="${name}-${version}.tar.gz"
      echo "packaging ${name}@${version} (python)" >&2
      ( cd "${plugin_dir}" && shopt -s nullglob && \
        tar -czf "${DIST_DIR}/${tarball}" \
          --exclude='.venv' --exclude='__pycache__' --exclude='*.pyc' \
          -- * )
      digest="$(sha256 "${DIST_DIR}/${tarball}")"
      {
        echo "          python:"
        echo "            url: \"${ASSET_BASE}/${tarball}\""
        echo "            sha256: \"${digest}\""
      } >>"${INDEX_OUT}.tmp"
      ;;

    go)
      binary="$(field binary "${manifest}")"
      if [ -z "${binary}" ]; then
        echo "skip: ${plugin_dir} go runtime missing entrypoint.go.binary" >&2
        continue
      fi
      for p in ${GO_PLATFORMS}; do
        os="${p%_*}"
        arch="${p#*_}"
        ext=""
        [ "${os}" = "windows" ] && ext=".exe"
        echo "packaging ${name}@${version} (go ${os}/${arch})" >&2
        ( cd "${plugin_dir}" && GOOS="${os}" GOARCH="${arch}" CGO_ENABLED=0 \
            go build -trimpath -ldflags="-s -w" -o "bin/${binary}${ext}" . )
        tarball="${name}-${version}-${p}.tar.gz"
        tar -czf "${DIST_DIR}/${tarball}" \
          -C "${plugin_dir}" plugin.yaml "bin/${binary}${ext}"
        digest="$(sha256 "${DIST_DIR}/${tarball}")"
        {
          echo "          go/${os}_${arch}:"
          echo "            url: \"${ASSET_BASE}/${tarball}\""
          echo "            sha256: \"${digest}\""
        } >>"${INDEX_OUT}.tmp"
      done
      ;;

    *)
      echo "skip: ${plugin_dir} unsupported runtime ${runtime}" >&2
      continue
      ;;
  esac
done

mv "${INDEX_OUT}.tmp" "${INDEX_OUT}"
cp "${INDEX_OUT}" "${DIST_DIR}/index.yaml"

echo "wrote ${INDEX_OUT}" >&2
echo "artifacts in ${DIST_DIR}" >&2
