<template>
  <div :class="embedded ? 'theme-authorization-settings' : 'h-full flex flex-col bg-gray-50'">
    <!-- Header -->
    <div v-if="!embedded" class="bg-white/80 backdrop-blur-md border-b border-gray-200 px-6 py-4 sticky top-0 z-20 shadow-sm transition-all duration-300">
      <div class="flex items-center gap-4">
        <h1 class="text-2xl font-bold text-gray-900 tracking-tight flex items-center gap-2">
          <Settings :size="28" class="text-blue-600" />
          系统设置
        </h1>
        <span class="text-sm text-gray-500 hidden md:inline-block border-l border-gray-200 pl-4 h-5 leading-5">
          管理 QQ 音乐登录状态和系统配置
        </span>
      </div>
    </div>

    <!-- Content -->
    <div :class="embedded ? '' : 'flex-1 overflow-y-auto px-4 md:px-6 py-4 md:py-6 pb-24 scrollbar-thin'">
      <div :class="embedded ? 'space-y-4' : 'max-w-4xl mx-auto space-y-6 md:space-y-8 fade-in'">
        <!-- Status Banner -->
        <div v-if="status" class="status-info animate-fade-in shadow-sm rounded-xl border-blue-100 bg-blue-50/50">
          <div class="flex-shrink-0">
            <Info :size="20" />
          </div>
          <span class="font-medium text-blue-700">{{ status }}</span>
        </div>

        <!-- QQ Music Admin Section -->
        <section class="theme-settings-panel bg-white rounded-lg border border-gray-200 shadow-sm overflow-hidden relative">
          <div v-if="!embedded" class="absolute top-0 right-0 p-6 opacity-[0.03] pointer-events-none">
            <Shield :size="200" class="text-black" />
          </div>

          <div class="p-5 md:p-8 relative z-10">
            <div class="flex items-center justify-between mb-6 md:mb-8">
              <div>
                <h2 class="text-xl font-bold text-gray-900 flex items-center gap-3">
                  QQ音乐后台授权
                  <span
                    :class="[
                      'text-xs px-2.5 py-1 rounded-full font-semibold border transition-colors',
                      qqAdminStatus
                        ? 'bg-green-50 text-green-700 border-green-200'
                        : 'bg-gray-100 text-gray-600 border-gray-200'
                    ]"
                  >
                    {{ qqAdminStatus ? '已保存' : '未配置' }}
                  </span>
                </h2>
                <p class="text-gray-500 text-sm mt-2">用于服务器端点歌播放（Cookie 加密存储在服务器，不做返回）</p>
              </div>
            </div>

            <div class="space-y-8">
              <div class="flex flex-col md:flex-row gap-8">
                <div class="flex-1 space-y-6">
                  <div class="flex flex-wrap gap-3">
                    <button @click="startQQAdminQr" :disabled="qqAdminDeleting || qqAdminQrStarting" class="btn-primary shadow-blue-200">
                      <QrCode :size="18" />
                      扫码授权
                    </button>
                    <button @click="load" class="btn-secondary">
                      <RefreshCw :size="18" />
                      刷新状态
                    </button>
                  </div>

                  <div v-if="qqAdminStatus" class="flex items-center gap-2 text-sm text-green-600 font-medium">
                    <CheckCircle2 :size="16" />
                    服务器已保存 Cookie，登录有效性未验证
                  </div>
                </div>

                <div
                  v-if="qqAdminQrImg"
                  class="flex-shrink-0 flex flex-col items-center gap-4 bg-white p-6 rounded-2xl border border-gray-200 shadow-lg shadow-gray-100 animate-scale-in"
                >
                  <img :src="qqAdminQrImg" alt="qq admin qr" class="w-48 h-48 object-contain rounded-lg" />
                  <span class="text-sm text-gray-500 font-medium flex items-center gap-1.5">
                    <Smartphone :size="16" />
                    请使用手机 QQ 扫码
                  </span>
                </div>
              </div>

              <!-- Manual Cookie Input -->
              <div class="pt-8 border-t border-gray-100">
                <h3 class="text-sm font-bold text-gray-900 mb-4 flex items-center gap-2">
                  <Terminal :size="16" class="text-gray-400" />
                  手动配置
                </h3>
                <div class="flex flex-col md:flex-row gap-3">
                  <input
                    v-model="qqAdminManualCookie"
                    type="text"
                    placeholder="输入 Cookie 字符串 (uin=...; p_skey=... 等)"
                    class="input-field flex-1 font-mono text-sm"
                  />
                  <button @click="setQQAdminCookie" :disabled="qqAdminDeleting || qqAdminSaving" class="btn-secondary whitespace-nowrap font-medium">
                    保存配置
                  </button>
                </div>
                <p class="text-xs text-gray-400 mt-3 flex items-center gap-1.5">
                  <AlertCircle :size="12" />
                  QQ音乐获取播放链接通常需要登录态 cookie。
                </p>
                <button @click="deleteQQAdminCookie" :disabled="qqAdminDeleting || qqAdminSaving || qqAdminQrStarting" class="btn-secondary mt-4 text-red-600">
                  {{ qqAdminDeleting ? '正在删除…' : '删除已保存的登录' }}
                </button>
              </div>
            </div>
          </div>
        </section>

      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { onMounted, onUnmounted, ref } from 'vue'
import { apiDelete, apiGet, apiPost } from '../api'
import { 
  Settings, 
  Info, 
  QrCode, 
  RefreshCw, 
  CheckCircle2, 
  Smartphone, 
  Shield, 
  Terminal, 
  AlertCircle 
} from 'lucide-vue-next'

withDefaults(defineProps<{ embedded?: boolean }>(), {
  embedded: false,
})

const qqAdminStatus = ref<boolean>(false)
const qqAdminQrKey = ref('')
const qqAdminQrImg = ref('')
const qqAdminPtqrtoken = ref('')
const qqAdminPtLoginSig = ref('')
const qqAdminAuthUrl = ref('')

const qqAdminManualCookie = ref('')
const qqAdminDeleting = ref(false)
const qqAdminSaving = ref(false)
const qqAdminQrStarting = ref(false)

const status = ref('')

let qqAdminTimer: number | null = null
const qqAdminChecks = new Set<Promise<void>>()

function getAdminHeaders(): Record<string, string> {
  return {}
}

async function load() {
  status.value = ''
  try {
    const qst = await apiGet<{ admin_cookie_set: boolean }>('/admin/qqmusic/status', getAdminHeaders())
    qqAdminStatus.value = !!qst?.admin_cookie_set
  } catch {
    qqAdminStatus.value = false
  }

}

async function startQQAdminQr() {
  status.value = ''
  qqAdminQrStarting.value = true
  try {
    stopQQAdminPoll()
    qqAdminQrImg.value = ''
    qqAdminAuthUrl.value = ''

    const keyRes = await apiGet<any>('/qqmusic/login/qr/key')
    const imgBase64 = String(keyRes?.qr_image_base64 || '')
    qqAdminQrImg.value = imgBase64 ? `data:image/png;base64,${imgBase64}` : String(keyRes?.qr_url || '')
    qqAdminQrKey.value = String(keyRes?.qr_key || '')
    qqAdminPtqrtoken.value = String(keyRes?.ptqrtoken || '')
    qqAdminPtLoginSig.value = String(keyRes?.pt_login_sig || '')

    if (!qqAdminQrKey.value || !qqAdminPtqrtoken.value) throw new Error('failed to get qqmusic qr key')

    status.value = 'qqmusic admin qr created'
    qqAdminTimer = window.setInterval(() => {
      const check = checkQQAdminQr()
      qqAdminChecks.add(check)
      void check.finally(() => qqAdminChecks.delete(check))
    }, 1500)
  } catch (e: any) {
    status.value = String(e?.message ?? e)
  } finally {
    qqAdminQrStarting.value = false
  }
}

async function checkQQAdminQr() {
  status.value = ''
  try {
    if (!qqAdminQrKey.value || !qqAdminPtqrtoken.value) return
    const r = await apiPost<any>('/qqmusic/login/qr/check', {
      qr_key: qqAdminQrKey.value,
      ptqrtoken: qqAdminPtqrtoken.value,
      pt_login_sig: qqAdminPtLoginSig.value,
    })
    const st = String(r?.status || '')

    if (st === 'waiting') {
      status.value = 'qqmusic qr waiting'
      return
    }
    if (st === 'scanning') {
      status.value = 'qqmusic qr scanned (confirm on phone)'
      return
    }
    if (st === 'expired') {
      status.value = 'qqmusic qr expired'
      stopQQAdminPoll()
      return
    }
    if (st === 'success') {
      const authUrl = String(r?.auth_url || '')
      if (!authUrl) throw new Error('authorized but auth_url is empty')

      qqAdminAuthUrl.value = authUrl
      await apiPost<any>('/admin/qqmusic/qr/confirm', { auth_url: authUrl }, getAdminHeaders())
      status.value = 'qqmusic admin authorized (cookie saved server-side)'
      stopQQAdminPoll()
      await load()
      return
    }

    status.value = st === 'error' ? String(r?.message || '检查二维码状态失败') : `qqmusic qr unknown: status=${st}`
  } catch (e: any) {
    status.value = String(e?.message ?? e)
  }
}

async function setQQAdminCookie() {
  status.value = ''
  qqAdminSaving.value = true
  try {
    const cookie = qqAdminManualCookie.value
    if (!cookie.trim()) throw new Error('cookie is empty')
    await apiPost<any>('/admin/qqmusic/cookie', { cookie }, getAdminHeaders())
    qqAdminManualCookie.value = ''
    status.value = 'qqmusic admin cookie saved server-side'
    await load()
  } catch (e: any) {
    status.value = String(e?.message ?? e)
  } finally {
    qqAdminSaving.value = false
  }
}

async function deleteQQAdminCookie() {
  if (!window.confirm('确定要删除保存的 QQ 音乐登录信息吗？删除后需要重新授权。')) return
  qqAdminDeleting.value = true
  stopQQAdminPoll()
  qqAdminQrImg.value = ''
  try {
    await Promise.all(qqAdminChecks)
    await apiDelete('/admin/qqmusic/cookie', getAdminHeaders())
    qqAdminManualCookie.value = ''
    qqAdminStatus.value = false
    status.value = '已删除保存的 QQ 音乐登录信息'
  } catch (e: any) {
    status.value = String(e?.message ?? e)
  } finally {
    qqAdminDeleting.value = false
  }
}

function stopQQAdminPoll() {
  if (qqAdminTimer !== null) {
    clearInterval(qqAdminTimer)
    qqAdminTimer = null
  }
}

onMounted(load)
onUnmounted(() => {
  stopQQAdminPoll()
})
</script>
