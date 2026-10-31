<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { Clock, Heart, ListMusic, Search } from 'lucide-vue-next'

const groups = [
  {
    title: '发现',
    items: [
      { path: '/search', label: '搜索', icon: Search },
      { path: '/playlists', label: '歌单广场', icon: ListMusic },
    ],
  },
  {
    title: '我的音乐',
    items: [
      { path: '/queue', label: '播放队列', icon: ListMusic },
      { path: '/favorites', label: '本地收藏', icon: Heart },
      { path: '/history', label: '最近播放', icon: Clock },
    ],
  },
]

const route = useRoute()
const navigation = ref<HTMLElement>()
const selection = ref<{ top: number; height: number }>()
const musicPath = (path: string) => path.startsWith('/playlist/') ? '/playlists' : path
const selectedPath = ref(musicPath(route.path))
const hasSelection = computed(() => groups.some(group => group.items.some(item => item.path === selectedPath.value)))

function moveSelection() {
  if (!hasSelection.value) return
  const item = navigation.value!.querySelector<HTMLAnchorElement>(`[data-nav-path="${selectedPath.value}"]`)!
  selection.value = { top: item.offsetTop, height: item.offsetHeight }
}

function selectItem(event: MouseEvent, path: string) {
  if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return
  selectedPath.value = path
}

watch(() => route.path, path => { selectedPath.value = musicPath(path) })
watch(selectedPath, moveSelection, { flush: 'post' })
onMounted(moveSelection)
</script>

<template>
  <nav class="flex-1 px-4 py-6 overflow-y-auto scrollbar-thin" aria-label="音乐导航">
    <div ref="navigation" class="relative">
      <div
        v-if="hasSelection && selection"
        class="sidebar-nav-selection"
        :style="{ transform: `translateY(${selection.top}px)`, height: `${selection.height}px` }"
        aria-hidden="true"
      ></div>
      <div v-for="(group, index) in groups" :key="group.title" :class="['space-y-1.5', index ? 'mt-8' : '']">
        <div :class="['relative px-3 text-xs font-semibold text-gray-400 uppercase tracking-wider mb-2', index ? '' : 'mt-1']">{{ group.title }}</div>
        <RouterLink
          v-for="item in group.items"
          :key="item.path"
          :to="item.path"
          :data-nav-path="item.path"
          :class="['sidebar-nav-link', { 'sidebar-nav-link-selected': selectedPath === item.path }]"
          @click="selectItem($event, item.path)"
        >
          <component :is="item.icon" :size="20" />
          <span>{{ item.label }}</span>
        </RouterLink>
      </div>
    </div>
  </nav>
</template>
