<script setup lang="ts">
import { ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { LockKeyhole, LogIn, Music, UserRound } from 'lucide-vue-next'
import { login, loginAsGuest } from '../auth'
import { appConfig } from '../appConfig'

const route = useRoute()
const router = useRouter()
const username = ref('admin')
const password = ref('')
const error = ref('')
const submitting = ref(false)
const guestSubmitting = ref(false)

async function enterAsGuest() {
  error.value = ''
  guestSubmitting.value = true
  try {
    await loginAsGuest()
    await router.replace('/search')
  } catch (e: any) {
    error.value = String(e?.message || e)
  } finally {
    guestSubmitting.value = false
  }
}

async function submit() {
  error.value = ''
  submitting.value = true
  try {
    const status = await login(username.value, password.value)
    if (status.must_change_password) {
      await router.replace('/change-password')
    } else {
      await router.replace(String(route.query.redirect || '/settings'))
    }
  } catch (e: any) {
    error.value = String(e?.message || e)
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <main class="theme-auth-shell min-h-[100dvh] bg-gray-50 flex items-center justify-center px-4 py-10">
    <section class="theme-auth-panel w-full max-w-sm bg-white border border-gray-200 rounded-lg shadow-sm p-7">
      <div class="flex items-center gap-3 mb-7">
        <div class="w-10 h-10 rounded-lg bg-blue-600 text-white flex items-center justify-center">
          <Music :size="21" />
        </div>
        <div>
          <h1 class="text-xl font-bold text-gray-900">{{ appConfig.name }}</h1>
          <p class="text-sm text-gray-500">管理员登录</p>
        </div>
      </div>

      <form class="space-y-5" @submit.prevent="submit">
        <label class="block">
          <span class="block text-sm font-medium text-gray-700 mb-1.5">用户名</span>
          <input v-model="username" autocomplete="username" class="input-field w-full" required />
        </label>
        <label class="block">
          <span class="block text-sm font-medium text-gray-700 mb-1.5">密码</span>
          <div class="relative">
            <LockKeyhole :size="17" class="absolute left-3 top-1/2 -translate-y-1/2 text-gray-400" />
            <input v-model="password" type="password" autocomplete="current-password" class="input-field w-full pl-10" required autofocus />
          </div>
        </label>
        <p v-if="error" class="text-sm text-red-600" role="alert">{{ error }}</p>
        <button type="submit" class="btn-primary w-full justify-center" :disabled="submitting || guestSubmitting">
          <LogIn :size="18" />
          {{ submitting ? '正在登录' : '登录' }}
        </button>
      </form>

      <div class="mt-6 pt-5 border-t border-gray-100">
        <button type="button" class="btn-secondary w-full justify-center" :disabled="submitting || guestSubmitting" @click="enterAsGuest">
          <UserRound :size="18" />
          {{ guestSubmitting ? '正在进入' : '访客登录' }}
        </button>
        <p class="mt-2 text-xs leading-5 text-gray-500">无需账号，可搜索歌曲、浏览歌单并加入点歌队列。后台设置仅对管理员开放。</p>
      </div>

      <p class="mt-6 pt-5 border-t border-gray-100 text-xs leading-5 text-gray-500">
        首次启动密码记录在后端启动日志和受保护的初始密码文件中。
      </p>
    </section>
  </main>
</template>
