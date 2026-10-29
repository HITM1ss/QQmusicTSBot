import { defineConfig, loadEnv, ProxyOptions } from 'vite'
import vue from '@vitejs/plugin-vue'

function normalizePort(value: string | undefined, fallback: number): number {
  const cleaned = (value || '').trim().replace(/^:/, '')
  const parsed = Number.parseInt(cleaned, 10)
  return Number.isFinite(parsed) ? parsed : fallback
}

function normalizeServerHost(value: string | undefined, fallback: string): string {
  const host = (value || fallback).trim()
  return host || fallback
}

function normalizeProxyTargetHost(value: string | undefined, fallback: string): string {
  const host = (value || fallback)
    .trim()
    .replace(/^https?:\/\//, '')
    .replace(/\/.*$/, '')
    .replace(/:.+$/, '')
  if (!host || host === '0.0.0.0') {
    return fallback
  }
  return host
}

function trimTrailingSlash(value: string): string {
  return value.replace(/\/+$/, '')
}

function parseAllowedHosts(value: string | undefined): string[] | undefined {
  const hosts = (value || '')
    .split(',')
    .map((item) => item.trim())
    .filter(Boolean)
  return hosts.length ? hosts : undefined
}

function parseHostnameFromUrl(value: string | undefined): string | undefined {
  const raw = (value || '').trim()
  if (!raw) return undefined
  try {
    return new URL(raw).hostname || undefined
  } catch {
    return undefined
  }
}

export default defineConfig(({ mode }) => {
  const env = { ...loadEnv(mode, process.cwd(), ''), ...process.env }

  const apiProxyTarget = trimTrailingSlash(
    (env.TSBOT_WEB_API_PROXY_TARGET || '').trim() ||
      `http://${normalizeProxyTargetHost(env.TSBOT_HOST, '127.0.0.1')}:${normalizePort(env.TSBOT_PORT, 8009)}`,
  )

  const apiProxy: ProxyOptions = {
    target: apiProxyTarget,
    changeOrigin: true,
    rewrite: (path) => path.replace(/^\/api/, ''),
  }

  const allowedHostsSet = new Set(parseAllowedHosts(env.TSBOT_WEB_ALLOWED_HOSTS) || [])
  const publicHostname = parseHostnameFromUrl(env.VITE_WEB_PUBLIC_URL)
  if (publicHostname) {
    allowedHostsSet.add(publicHostname)
  }
  const allowedHosts = allowedHostsSet.size ? Array.from(allowedHostsSet) : undefined
  const buildInfo = { version: env.VITE_TSBOT_VERSION || 'development', revision: env.VITE_TSBOT_REVISION || '' }

  return {
    define: { __TSBOT_BUILD_INFO__: JSON.stringify(buildInfo) },
    plugins: [vue(), {
      name: 'tsbot-build-info',
      generateBundle() {
        this.emitFile({ type: 'asset', fileName: 'version.json', source: JSON.stringify(buildInfo) })
      },
    }],
    server: {
      host: normalizeServerHost(env.VITE_DEV_HOST, '127.0.0.1'),
      port: normalizePort(env.VITE_DEV_PORT, 5173),
      allowedHosts,
      proxy: {
        '/api': apiProxy,
      },
    },
    preview: {
      host: normalizeServerHost(env.TSBOT_WEB_HOST, '127.0.0.1'),
      port: normalizePort(env.TSBOT_WEB_PORT, 8080),
      allowedHosts,
      proxy: {
        '/api': apiProxy,
      },
    },
  }
})
