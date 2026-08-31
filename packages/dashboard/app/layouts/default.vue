<script setup lang="ts">
const colorMode = useState<'dark' | 'light'>('colorMode', () => 'dark');

function applyTheme() {
  document.documentElement.setAttribute(
    'data-theme',
    colorMode.value === 'dark' ? 'rca-ops' : 'rca-ops-light',
  );
}

function toggleTheme() {
  colorMode.value = colorMode.value === 'dark' ? 'light' : 'dark';
  if (import.meta.client) applyTheme();
}

onMounted(applyTheme);
</script>

<template>
  <div class="app-shell">
    <aside class="app-sidebar">
      <NuxtLink to="/" class="app-brand">
        <span class="app-brand-mark" aria-hidden="true" />
        <span class="min-w-0">
          <span class="block text-[13px] font-bold tracking-[-0.02em]">
            RCA Control
          </span>
          <span
            class="app-brand-subtitle block mt-0.5 text-[9px] font-mono uppercase tracking-[0.12em] text-base-content/45"
          >
            Incident Operations
          </span>
        </span>
      </NuxtLink>

      <div class="app-nav-label">Workspace</div>
      <NuxtLink to="/" class="app-nav-link">
        <svg
          class="size-4"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          stroke-width="1.8"
          aria-hidden="true"
        >
          <path d="M4 5h16v5H4zM4 14h7v5H4zM15 14h5v5h-5z" />
        </svg>
        <span>Incident Queue</span>
      </NuxtLink>

      <div class="app-sidebar-foot">
        <div class="flex items-center gap-2 text-[10px] text-base-content/50">
          <span class="size-1.5 rounded-full bg-success" aria-hidden="true" />
          <span class="font-mono uppercase tracking-[0.08em]"
            >Local console</span
          >
        </div>
        <p class="mt-2 text-[10px] leading-relaxed text-base-content/38">
          분석은 읽기 전용이며 복구는 승인 후 별도 워커가 실행합니다.
        </p>
      </div>
    </aside>

    <div class="app-main-column">
      <header class="app-topbar">
        <span
          class="hidden sm:inline-flex items-center gap-2 text-[10px] font-mono uppercase tracking-[0.08em] text-base-content/45"
        >
          <span class="size-1.5 rounded-full bg-success" aria-hidden="true" />
          Operator workspace
        </span>
        <button
          class="btn btn-ghost btn-sm btn-square"
          :aria-label="
            colorMode === 'dark' ? '밝은 화면으로 전환' : '어두운 화면으로 전환'
          "
          @click="toggleTheme()"
        >
          <svg
            v-if="colorMode === 'dark'"
            class="size-4"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            stroke-width="1.8"
            aria-hidden="true"
          >
            <circle cx="12" cy="12" r="4.5" />
            <path
              stroke-linecap="round"
              d="M12 2.5v2M12 19.5v2M2.5 12h2M19.5 12h2M5.3 5.3l1.4 1.4M17.3 17.3l1.4 1.4M18.7 5.3l-1.4 1.4M6.7 17.3l-1.4 1.4"
            />
          </svg>
          <svg
            v-else
            class="size-4"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            stroke-width="1.8"
            aria-hidden="true"
          >
            <path
              stroke-linecap="round"
              stroke-linejoin="round"
              d="M20.5 13.3A8.6 8.6 0 1110.7 3.5a6.7 6.7 0 009.8 9.8z"
            />
          </svg>
        </button>
      </header>

      <main class="app-content">
        <slot />
      </main>
    </div>
  </div>
</template>
