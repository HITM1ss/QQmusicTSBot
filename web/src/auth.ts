import { computed, reactive } from 'vue'
import { apiGet, apiPost } from './api'
import { setCsrfToken } from './session'

export interface AuthStatus {
  initialized: boolean
  authenticated: boolean
  role: 'none' | 'guest' | 'admin'
  must_change_password: boolean
  username: string
  csrf_token: string
}

export const authState = reactive<AuthStatus>({
  initialized: false,
  authenticated: false,
  role: 'none',
  must_change_password: false,
  username: '',
  csrf_token: '',
})

export const isAdmin = computed(() => authState.role === 'admin')

let loaded = false

function applyStatus(status: Partial<AuthStatus>): AuthStatus {
  Object.assign(authState, {
    initialized: status.initialized ?? true,
    authenticated: !!status.authenticated,
    role: status.role || (status.authenticated ? 'admin' : 'none'),
    must_change_password: !!status.must_change_password,
    username: status.username || '',
    csrf_token: status.csrf_token || '',
  })
  setCsrfToken(authState.csrf_token)
  loaded = true
  return authState
}

export async function refreshAuth(force = false): Promise<AuthStatus> {
  if (loaded && !force) return authState
  return applyStatus(await apiGet<AuthStatus>('/auth/status'))
}

export async function login(username: string, password: string): Promise<AuthStatus> {
  return applyStatus(await apiPost<AuthStatus>('/auth/login', { username, password }))
}

export async function loginAsGuest(): Promise<AuthStatus> {
  return applyStatus(await apiPost<AuthStatus>('/auth/guest', {}))
}

export async function changePassword(currentPassword: string, newPassword: string): Promise<AuthStatus> {
  return applyStatus(await apiPost<AuthStatus>('/auth/change-password', {
    current_password: currentPassword,
    new_password: newPassword,
  }))
}

export async function logout(): Promise<void> {
  await apiPost('/auth/logout', {})
  applyStatus({ initialized: true, authenticated: false })
}
