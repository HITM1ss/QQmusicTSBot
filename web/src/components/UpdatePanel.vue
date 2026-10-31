<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { apiGet, apiPost } from '../api'

declare const __TSBOT_BUILD_INFO__: { version: string; revision: string }
const frontend = __TSBOT_BUILD_INFO__
const backend = ref<{ version: string; revision: string } | null>(null)
const latest = ref<{ version: string; url: string } | null>(null)
const updateAvailable = ref<boolean | null>(null)
type ImageSource = 'ghcr' | 'nju'
const imageSource = ref<ImageSource>('ghcr')
const sourceLabel = computed(() => imageSource.value === 'ghcr' ? '原地址（GHCR）' : '南大源')
const updater = ref<{ available: boolean; state: string; message: string; version?: string; progress?: number | null; image_source?: ImageSource } | null>(null)
const checking = ref(false)
const submitting = ref(false)
const error = ref('')
let timer: number | undefined
let alive = true
let refreshDeadline = 0
const updating = computed(() => submitting.value || ['pending', 'running'].includes(updater.value?.state || ''))
const mismatched = computed(() => backend.value && frontend.version !== backend.value.version)

function versionLabel(version?: string) {
  return !version || version === 'development' ? '开发版' : version
}

async function refresh() {
  const results = await Promise.allSettled([
    apiGet<{ version: string; revision: string }>('/admin/version'),
    apiGet<NonNullable<typeof updater.value>>('/admin/update/status'),
  ])
  if (!alive) return
  if (results[0].status === 'fulfilled') {
    backend.value = results[0].value
    error.value = ''
  } else if (!updating.value) error.value = '无法读取后端版本，请检查服务连接'
  if (results[1].status === 'fulfilled') {
    const status = results[1].value
    if (status.image_source && ['pending', 'running'].includes(status.state)) {
      imageSource.value = status.image_source
    }
    updater.value = results[1].value
    if (['pending', 'running'].includes(updater.value.state)) scheduleRefresh()
  }
}

function scheduleRefresh() {
  window.clearTimeout(timer)
  if (!refreshDeadline) refreshDeadline = Date.now() + 60 * 60 * 1000
  if (Date.now() > refreshDeadline) {
    error.value = '等待更新超时，请查看宿主机更新服务日志，并手动刷新状态'
    return
  }
  if (alive) timer = window.setTimeout(async () => {
    try { await refresh() } catch { /* Services may be restarting. */ }
    if (alive && updating.value) scheduleRefresh()
  }, 3000)
}

async function checkUpdate() {
  checking.value = true
  error.value = ''
  try {
    const result = await apiGet<{ current: NonNullable<typeof backend.value>; latest: NonNullable<typeof latest.value>; update_available: boolean | null }>('/admin/update/check')
    backend.value = result.current
    latest.value = result.latest
    updateAvailable.value = result.update_available
  } catch (e: any) {
    error.value = String(e?.message || e)
  } finally {
    checking.value = false
  }
}

async function startUpdate() {
  if (!window.confirm(`从${sourceLabel.value}更新到 ${latest.value?.version}？更新前会备份数据库，服务将短暂中断。`)) return
  submitting.value = true
  error.value = ''
  try {
    const result = await apiPost<{ version: string; image_source: ImageSource }>('/admin/update', { version: latest.value?.version, image_source: imageSource.value })
    refreshDeadline = 0
    updater.value = { available: true, state: 'pending', message: '更新请求已提交', version: result.version, image_source: result.image_source }
    scheduleRefresh()
  } catch (e: any) {
    error.value = String(e?.message || e)
  } finally {
    submitting.value = false
  }
}

function reloadPage() { window.location.reload() }

onMounted(refresh)
onUnmounted(() => { alive = false; window.clearTimeout(timer) })
</script>

<template>
  <section class="theme-settings-panel bg-white border border-gray-200 rounded-lg p-5 space-y-5">
    <dl class="grid grid-cols-1 sm:grid-cols-2 gap-4">
      <div>
        <dt class="text-sm text-gray-500">前端版本</dt>
        <dd class="mt-1 font-mono text-gray-900">{{ versionLabel(frontend.version) }} <span v-if="frontend.revision" class="text-xs text-gray-400">{{ frontend.revision.slice(0, 7) }}</span></dd>
      </div>
      <div>
        <dt class="text-sm text-gray-500">后端版本</dt>
        <dd class="mt-1 font-mono text-gray-900">{{ backend ? versionLabel(backend.version) : '读取中…' }} <span v-if="backend?.revision" class="text-xs text-gray-400">{{ backend.revision.slice(0, 7) }}</span></dd>
      </div>
    </dl>
    <p v-if="mismatched" class="text-sm text-amber-700">前后端版本不同，请确认服务已更新并刷新页面。</p>
    <div class="border-t border-gray-100 pt-4 space-y-3">
      <p v-if="latest" class="text-sm text-gray-700">
        最新稳定版：<a :href="latest.url" target="_blank" rel="noopener noreferrer" class="text-blue-600 underline">{{ latest.version }}</a>
        <span class="ml-2">{{ updateAvailable === null ? '开发版无法自动比较版本' : updateAvailable ? '有可用更新' : '后端已是最新版本' }}</span>
      </p>
      <p v-if="updater" class="text-sm" :class="updater.state === 'failed' ? 'text-red-600' : 'text-gray-500'">{{ updater.message }}<span v-if="updater.version">（{{ updater.version }}）</span><span v-if="updater.progress != null">...{{ updater.progress }}%</span></p>
      <p v-if="updater && !updater.available" class="text-xs text-gray-500">在 Linux 宿主机的部署目录运行 sudo bash scripts/install-updater.sh，完成一次性安装后即可使用一键更新。</p>
      <p v-if="error" class="text-sm text-red-600">{{ error }}</p>
      <fieldset :disabled="updating" class="space-y-2">
        <legend class="mb-2 text-sm font-medium text-gray-700">从何处拉取镜像</legend>
        <label class="flex items-center gap-2 text-sm text-gray-700 cursor-pointer">
          <input v-model="imageSource" type="radio" name="update-image-source" value="ghcr" class="h-4 w-4 accent-blue-600" />
          <span>原地址（GHCR）</span>
        </label>
        <label class="flex items-center gap-2 text-sm text-gray-700 cursor-pointer">
          <input v-model="imageSource" type="radio" name="update-image-source" value="nju" class="h-4 w-4 accent-blue-600" />
          <span>南大源</span>
        </label>
        <p class="text-xs text-gray-500">{{ imageSource === 'ghcr' ? '拉取失败或超过 10 分钟后自动改用南大源，南大源最多等待 40 分钟。' : '本次更新直接从南大源拉取镜像，最多等待 40 分钟。' }}</p>
      </fieldset>
      <div class="flex flex-wrap gap-3">
        <button class="btn-secondary" :disabled="checking || updating" @click="checkUpdate">{{ checking ? '正在检查…' : '检查更新' }}</button>
        <button class="btn-primary" :disabled="!updater?.available || !updateAvailable || updating" @click="startUpdate">{{ updating ? '正在更新…' : '一键更新' }}</button>
        <button class="btn-secondary" @click="refresh">刷新状态</button>
        <button v-if="updater?.state === 'success'" class="btn-secondary" @click="reloadPage">刷新页面</button>
      </div>
    </div>
  </section>
</template>
