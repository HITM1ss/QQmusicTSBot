<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { apiGet, apiPost } from '../api'
import { isAdmin } from '../auth'
import { 
  Search, 
  Play, 
  Plus, 
  Heart, 
  Clock,
  Music,
  Loader2,
  User,
  Settings
} from 'lucide-vue-next'
import EmptyState from '../components/EmptyState.vue'
import LoadingSpinner from '../components/LoadingSpinner.vue'
import FloatingErrorToast from '../components/FloatingErrorToast.vue'
import { getFavoriteSongKey, getFavoriteSongs, isFavoriteSong, toggleFavoriteSong } from '../utils/favorites'
import { useTransientMessage } from '../composables/useTransientMessage'

const keywords = ref('')
const error = ref('')
const status = ref('')
const songs = ref<any[]>([])
const loading = ref(false)
const isSearchFocused = ref(false)
const qqMusicConfigured = ref(false)
const { message: actionError, showMessage: showActionError } = useTransientMessage()

// 搜索历史和分页
const searchHistory = ref<string[]>([])
const currentPage = ref(1)
const pageSize = ref(20)
const hasMore = ref(false)
const loadingMore = ref(false)

const favoriteSongKeys = ref<Set<string>>(new Set())

function refreshFavoriteSongIds() {
  favoriteSongKeys.value = new Set(getFavoriteSongs().map((song) => getFavoriteSongKey(song)).filter(Boolean))
}

function isLocalFav(song: any): boolean {
  const key = getFavoriteSongKey(song)
  if (!key) return false
  return favoriteSongKeys.value.has(key) || isFavoriteSong(song)
}

function toggleLocalFav(song: any) {
  toggleFavoriteSong(song)
  refreshFavoriteSongIds()
}

// Check QQ Music admin configuration status
async function checkQQMusicConfigStatus() {
  if (!isAdmin.value) return
  try {
    const status = await apiGet<any>('/admin/qqmusic/status')
    qqMusicConfigured.value = !!status?.admin_cookie_set
  } catch (e) {
    qqMusicConfigured.value = false
  }
}

// 搜索历史管理
function loadSearchHistory() {
  try {
    const history = localStorage.getItem('search-history')
    if (history) {
      searchHistory.value = JSON.parse(history)
    }
  } catch (e) {
    searchHistory.value = []
  }
}

function saveSearchHistory() {
  try {
    localStorage.setItem('search-history', JSON.stringify(searchHistory.value))
  } catch (e) {
    // 忽略存储错误
  }
}

function addToSearchHistory(keyword: string) {
  if (!keyword.trim()) return
  
  // 移除重复项
  const filtered = searchHistory.value.filter(item => item !== keyword)
  // 添加到开头
  searchHistory.value = [keyword, ...filtered].slice(0, 10) // 保留最近10条
  saveSearchHistory()
}

function clearSearchHistory() {
  searchHistory.value = []
  saveSearchHistory()
}

async function search(isLoadMore = false) {
  if (!keywords.value.trim()) return
  
  if (!isLoadMore) {
    loading.value = true
    error.value = ''
    status.value = ''
    songs.value = []
    currentPage.value = 1
    // 添加到搜索历史
    addToSearchHistory(keywords.value.trim())
  } else {
    loadingMore.value = true
    // 加载更多时先增加页码
    currentPage.value++
  }
  
  try {
    const res = await apiGet<{ songs: any[] }>(`/qqmusic/search/songs?keywords=${encodeURIComponent(keywords.value)}&limit=${pageSize.value}&page=${currentPage.value}`)
    const newSongs = res?.songs || []

    if (isLoadMore) {
      songs.value = [...songs.value, ...newSongs]
    } else {
      songs.value = newSongs
    }

    hasMore.value = newSongs.length === pageSize.value
  } catch (e: any) {
    error.value = String(e?.message ?? e)
    // 如果加载更多失败，回退页码
    if (isLoadMore) {
      currentPage.value--
    }
  } finally {
    loading.value = false
    loadingMore.value = false
  }
}

async function loadMore() {
  if (loadingMore.value || !hasMore.value) return
  await search(true)
}

function selectSuggestion(suggestion: string) {
  keywords.value = suggestion
  search()
}

async function enqueue(song: any, playNow: boolean) {
  error.value = ''
  status.value = ''
  try {
    const res = await apiPost<{ ok: boolean; id: number; source_url: string }>('/queue/qqmusic', {
      song_mid: String(song.mid || song.songmid || song.song_mid),
      title: getSongTitle(song),
      artist: getSongArtist(song),
      album: getSongAlbum(song),
      play_now: playNow,
      quality: "320",
      album_mid: String(song.album?.mid || song.albummid || ""),
      cover_url: getSongArtwork(song),
      duration_ms: getSongDurationMs(song),
    })
    status.value = `已添加到播放队列 #${res.id}${playNow ? ' (正在播放)' : ''}`
    
    // Clear status after 3 seconds
    setTimeout(() => {
      status.value = ''
    }, 3000)
  } catch (e: any) {
    const msg = String(e?.message ?? e)
    showActionError(playNow ? `点歌失败: ${msg}` : `添加到队列失败: ${msg}`)
  }
}

function formatDuration(duration: number): string {
  const minutes = Math.floor(duration / 60000)
  const seconds = Math.floor((duration % 60000) / 1000)
  return `${minutes}:${seconds.toString().padStart(2, '0')}`
}

function parseDurationToMs(value: any): number | undefined {
  if (typeof value === 'number' && Number.isFinite(value) && value > 0) {
    return value > 1000 ? value : value * 1000
  }

  const raw = String(value ?? '').trim()
  if (!raw) return undefined
  if (/^\d+$/.test(raw)) {
    const seconds = Number(raw)
    return Number.isFinite(seconds) && seconds > 0 ? seconds * 1000 : undefined
  }

  const parts = raw.split(':').map((part) => Number(part))
  if (!parts.length || parts.some((part) => !Number.isFinite(part) || part < 0)) {
    return undefined
  }

  let seconds = 0
  for (const part of parts) {
    seconds = seconds * 60 + part
  }
  return seconds > 0 ? seconds * 1000 : undefined
}

function getSongKey(song: any): string {
  return String(
    song?.track_id ||
    song?.id ||
    song?.songmid ||
    song?.mid ||
    song?.name ||
    song?.title ||
    ''
  )
}

function getSongTitle(song: any): string {
  return String(song?.name || song?.songname || song?.title || song?.songorig || '').trim()
}

function getSongArtist(song: any): string {
  const artists = song?.singer || song?.artists || song?.artist
  if (Array.isArray(artists)) {
    return artists.map((a: any) => a?.name || a).filter(Boolean).join(', ')
  }
  return String(artists || '').trim()
}

function getSongAlbum(song: any): string {
  return String(song?.album?.name || song?.album?.title || song?.albumname || song?.album || '').trim()
}

function getSongArtwork(song: any): string {
  const albumMid = String(song?.album?.pmid || song?.album?.mid || song?.albummid || '').trim()
  return albumMid ? `https://y.gtimg.cn/music/photo_new/T002R300x300M000${albumMid}.jpg` : ''
}

function getSongDurationMs(song: any): number | undefined {
  if (typeof song?.interval === 'number' && Number.isFinite(song.interval) && song.interval > 0) {
    return song.interval * 1000
  }
  return parseDurationToMs(song?.duration)
}

function formatSongDuration(song: any): string {
  const durationMs = getSongDurationMs(song)
  return durationMs ? formatDuration(durationMs) : '--:--'
}

function getSearchPlaceholder(): string {
  return '搜索QQ音乐歌曲、歌手或专辑...'
}

function handleKeyPress(event: KeyboardEvent) {
  if (event.key === 'Enter') {
    search()
  }
}

function handleFocus() {
  isSearchFocused.value = true
}

function handleBlur() {
  // 延迟隐藏建议，允许点击建议项
  setTimeout(() => {
    isSearchFocused.value = false
  }, 200)
}

onMounted(() => {
  refreshFavoriteSongIds()
  checkQQMusicConfigStatus()
  loadSearchHistory()
})
</script>

<template>
  <div class="h-full flex flex-col bg-gray-50">
    <!-- Header -->
    <div class="bg-white/80 backdrop-blur-md border-b border-gray-200 px-6 py-4 sticky top-0 z-30 shadow-sm transition-all duration-300">
      <h1 class="text-2xl font-bold text-gray-900 mb-4 flex items-center gap-2">
        <Search :size="28" class="text-blue-600" />
        {{ isAdmin ? '搜索媒体' : '访客点歌' }}
      </h1>
      
      <div v-if="isAdmin" class="mb-4 flex justify-end">
        <div class="flex items-center gap-2">
          <div v-if="qqMusicConfigured" class="flex items-center gap-2 text-sm text-green-600">
            <User :size="16" />
            <span>已配置</span>
          </div>
          <a
            v-else
            href="/cookie"
            class="flex items-center gap-1 px-3 py-1.5 text-sm font-medium text-blue-600 hover:text-blue-700 hover:bg-blue-50 rounded-lg transition-colors"
          >
            <Settings :size="16" />
            配置QQ音乐
          </a>
        </div>
      </div>
      
      <!-- Search bar -->
      <div class="relative max-w-2xl">
        <div class="relative group">
          <Search :size="20" class="absolute left-3 top-1/2 transform -translate-y-1/2 text-gray-400 group-focus-within:text-blue-500 transition-colors" />
          <input
            v-model="keywords"
            type="text"
            :placeholder="getSearchPlaceholder()"
            class="w-full pl-10 pr-24 py-3 bg-gray-100/50 border border-gray-200 rounded-xl text-gray-900 focus:bg-white focus:ring-2 focus:ring-blue-100 focus:border-blue-500 outline-none transition-all duration-200 placeholder-gray-400 text-sm font-medium"
            @keypress="handleKeyPress"
            @focus="handleFocus"
            @blur="handleBlur"
          />
          <button
            @click="() => search()"
            :disabled="loading || !keywords.trim()"
            class="absolute right-2 top-1/2 transform -translate-y-1/2 bg-blue-600 hover:bg-blue-700 text-white px-4 py-1.5 rounded-lg text-sm font-medium transition-colors disabled:opacity-50 disabled:cursor-not-allowed shadow-sm"
          >
            <Loader2 v-if="loading" :size="16" class="animate-spin" />
            <span v-else>搜索</span>
          </button>
        </div>
        
        <!-- Search suggestions and history -->
        <transition name="search-pop">
          <div 
            v-if="isSearchFocused && !keywords.trim() && searchHistory.length > 0"
            class="absolute top-full left-0 right-0 mt-2 bg-white/95 backdrop-blur-xl border border-gray-100/50 rounded-xl shadow-xl z-50 max-h-[320px] overflow-y-auto py-2 ring-1 ring-black/5"
          >
            <!-- Search history -->
            <div v-if="!keywords.trim() && searchHistory.length > 0">
              <div class="px-4 py-2 text-xs font-medium text-gray-500 uppercase tracking-wide flex items-center justify-between">
                <span>搜索历史</span>
                <button 
                  @click="clearSearchHistory"
                  class="text-gray-400 hover:text-red-500 transition-colors"
                  title="清除历史"
                >
                  <span class="text-xs">清除</span>
                </button>
              </div>
              <div
                v-for="historyItem in searchHistory"
                :key="historyItem"
                @click="selectSuggestion(historyItem)"
                class="px-4 py-2.5 hover:bg-gray-50/50 cursor-pointer flex items-center gap-3 group transition-colors"
              >
                <Clock :size="16" class="text-gray-400 group-hover:text-gray-600" />
                <span class="text-gray-600 group-hover:text-gray-900">{{ historyItem }}</span>
              </div>
            </div>
          </div>
        </transition>
      </div>
    </div>

    <!-- Content -->
    <div class="flex-1 overflow-y-auto px-4 md:px-6 py-4 md:py-6 pb-24 scrollbar-thin">
      <!-- Status messages -->
      <div v-if="error" class="mb-6 bg-red-50 border border-red-200 rounded-xl p-4 flex items-center gap-3 text-red-700">
        <div class="font-medium">搜索失败</div>
        <div class="text-sm opacity-90 border-l border-red-200 pl-3">{{ error }}</div>
      </div>
      
      <div v-if="status" class="mb-6 bg-green-50 border border-green-200 rounded-xl p-4 flex items-center gap-3 text-green-700 status-success shadow-sm">
        <div class="font-medium">{{ status }}</div>
      </div>
      
      <!-- Loading state -->
      <div v-if="loading" class="h-64 flex items-center justify-center">
         <LoadingSpinner :size="40" text="正在搜索..." />
      </div>
      
      <!-- Empty state -->
      <EmptyState
        v-else-if="!songs.length && !error && keywords && !loading"
        :icon="Music"
        title="未找到相关内容"
        description="尝试使用不同的关键词重新搜索"
        action-text="清除搜索条件"
        @action="keywords = ''"
      />
      
      <!-- Initial state -->
      <div v-else-if="!songs.length && !keywords" class="space-y-8 max-w-4xl mx-auto mt-4">
        <div class="text-center py-8">
           <div class="w-16 h-16 bg-blue-50 rounded-2xl flex items-center justify-center mx-auto mb-4 text-blue-500">
             <Search :size="32" />
           </div>
           <h2 class="text-lg font-bold text-gray-900">开始搜索内容</h2>
           <p class="text-gray-500 text-sm mt-1">{{ isAdmin ? '搜索歌曲、加入队列或直接播放' : '搜索喜欢的歌曲，点击加号加入点歌队列' }}</p>
        </div>
        
      </div>
      
      <!-- Search results -->
      <div v-else class="max-w-6xl mx-auto space-y-4 fade-in">
        <div class="flex items-center justify-between px-1">
          <h3 class="text-lg font-bold text-gray-900">搜索结果</h3>
          <span class="text-sm text-gray-500 bg-gray-100 px-2 py-1 rounded-md font-mono">
            {{ songs.length }} results
          </span>
        </div>
        

        <div class="bg-white rounded-2xl border border-gray-200 shadow-sm overflow-hidden">
          <table class="w-full text-left border-collapse">
            <thead class="bg-gray-50/50 text-gray-400 text-xs uppercase font-semibold border-b border-gray-100 hidden md:table-header-group">
              <tr>
                <th class="px-3 md:px-6 py-4 font-medium w-16">#</th>
                <th class="px-3 md:px-6 py-4 font-medium">标题</th>
                <th class="px-3 md:px-6 py-4 font-medium hidden md:table-cell">歌手</th>
                <th class="px-3 md:px-6 py-4 font-medium hidden lg:table-cell">专辑</th>
                <th class="px-3 md:px-6 py-4 font-medium w-24 text-right">时长</th>
                <th class="px-3 md:px-6 py-4 font-medium w-24"></th>
              </tr>
            </thead>
            <tbody class="divide-y divide-gray-50">
              <tr
                v-for="(song, index) in songs"
                :key="getSongKey(song)"
                class="group hover:bg-blue-50/30 transition-colors duration-200"
              >
                <td class="px-3 md:px-6 py-3 md:py-4 text-sm text-gray-400 text-center font-medium group-hover:text-blue-600 w-10 md:w-16">
                  {{ index + 1 }}
                </td>
                <td class="px-3 md:px-6 py-3 md:py-4">
                  <div class="flex items-center gap-3 md:gap-4">
                    <!-- Thumbnail -->
                    <div class="w-10 h-10 rounded-lg overflow-hidden flex-shrink-0 relative group/cover shadow-sm bg-gray-100">
                      <img 
                        v-if="getSongArtwork(song)"
                        :src="getSongArtwork(song)"
                        :alt="getSongTitle(song)"
                        class="w-full h-full object-cover"
                      />
                      <div v-else class="w-full h-full bg-gradient-to-br from-blue-100 to-indigo-100 flex items-center justify-center">
                        <Music :size="16" class="text-blue-400" />
                      </div>
                      <div 
                        class="absolute inset-0 bg-black/10 opacity-0 group-hover/cover:opacity-100 flex items-center justify-center transition-all cursor-pointer backdrop-blur-[1px]"
                        v-if="isAdmin"
                        @click="enqueue(song, true)"
                      >
                        <Play :size="16" class="text-white drop-shadow-md" fill="currentColor" />
                      </div>
                    </div>
                    
                    <div class="min-w-0">
                      <div class="font-semibold text-gray-900 line-clamp-1 text-sm group-hover:text-blue-600 transition-colors" :title="getSongTitle(song)">{{ getSongTitle(song) }}</div>
                      <div class="text-xs text-gray-500 md:hidden line-clamp-1 mt-0.5">
                        {{ getSongArtist(song) }}
                      </div>
                    </div>
                  </div>
                </td>
                <td class="px-3 md:px-6 py-3 md:py-4 text-sm text-gray-600 hidden md:table-cell">
                   <div class="truncate max-w-[150px]" :title="getSongArtist(song)">
                      {{ getSongArtist(song) }}
                   </div>
                </td>
                <td class="px-3 md:px-6 py-3 md:py-4 text-sm text-gray-500 hidden lg:table-cell">
                  <div class="truncate max-w-[150px]" :title="getSongAlbum(song)">{{ getSongAlbum(song) }}</div>
                </td>
                <td class="px-3 md:px-6 py-3 md:py-4 text-right text-sm text-gray-400 font-mono tabular-nums">
                  {{ formatSongDuration(song) }}
                </td>
                <td class="px-3 md:px-6 py-3 md:py-4 text-right">
                  <div class="flex items-center justify-end gap-2 md:opacity-0 group-hover:opacity-100 transition-all duration-200 md:transform md:translate-x-2 group-hover:translate-x-0">
                    <button
                      @click="enqueue(song, false)"
                      class="p-2 text-gray-400 hover:text-blue-600 hover:bg-blue-50 rounded-lg transition-colors"
                      title="添加到队列"
                    >
                      <Plus :size="18" />
                    </button>
                    
                    <button
                      @click.stop="toggleLocalFav(song)"
                      :class="[
                        'p-2 rounded-lg transition-colors',
                        isLocalFav(song)
                          ? 'text-pink-600 bg-pink-50 hover:bg-pink-100'
                          : 'text-gray-400 hover:text-pink-600 hover:bg-pink-50'
                      ]"
                      :title="isLocalFav(song) ? '取消本地收藏' : '本地收藏'"
                    >
                      <Heart :size="18" :fill="isLocalFav(song) ? 'currentColor' : 'none'" />
                    </button>
                  </div>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
        
        <!-- Load more button -->
        <div v-if="songs.length > 0 && hasMore" class="mt-6 flex justify-center">
          <button
            @click="loadMore"
            :disabled="loadingMore"
            class="px-6 py-3 bg-white border border-gray-200 rounded-xl text-gray-700 hover:bg-gray-50 hover:border-gray-300 transition-all duration-200 shadow-sm disabled:opacity-50 disabled:cursor-not-allowed flex items-center gap-2"
          >
            <Loader2 v-if="loadingMore" :size="16" class="animate-spin" />
            <span>{{ loadingMore ? '加载中...' : '加载更多' }}</span>
          </button>
        </div>
        
        <!-- Results info -->
        <div v-if="songs.length > 0" class="mt-4 text-center text-sm text-gray-500">
          已显示 {{ songs.length }} 条结果
          <span v-if="!hasMore">（已全部加载）</span>
        </div>
      </div>
    </div>

  </div>
  <FloatingErrorToast :message="actionError" />
</template>
