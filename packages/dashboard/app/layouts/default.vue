<script setup lang="ts">
const colorMode = useState('colorMode', () => 'light');

function applyTheme() {
  document.documentElement.setAttribute(
    'data-theme',
    colorMode.value === 'dark' ? 'workflow-night' : 'workflow',
  );
}

function toggleTheme() {
  colorMode.value = colorMode.value === 'dark' ? 'light' : 'dark';
  if (import.meta.client) applyTheme();
}

onMounted(applyTheme);
</script>

<template>
  <div class="min-h-screen bg-base-200">
    <header class="site-head sticky top-0 z-50">
      <!-- The bar centres its contents, and the wordmark keeps its own baseline
           relationship with the tagline. Aligning the whole bar on the baseline
           instead pins the text to the top edge, because a baseline group ignores
           the fixed height it sits in. -->
      <div
        class="max-w-[1160px] mx-auto px-5 sm:px-8 h-[52px] flex items-center gap-4"
      >
        <div class="flex items-baseline gap-3 min-w-0">
          <NuxtLink
            to="/"
            class="font-serif text-[19px] tracking-tight hover:text-primary transition-colors"
          >
            장애 기록
          </NuxtLink>
          <span class="label-sm hidden sm:inline">근본원인 분석</span>
        </div>
        <div class="flex-1" />
        <button
          class="btn btn-ghost btn-xs btn-square"
          :aria-label="
            colorMode === 'dark' ? '주간 화면으로 전환' : '야간 화면으로 전환'
          "
          @click="toggleTheme()"
        >
          <svg
            v-if="colorMode === 'dark'"
            class="size-[15px]"
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
            class="size-[15px]"
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
      </div>
    </header>
    <main class="max-w-[1160px] mx-auto px-5 sm:px-8 py-9">
      <slot />
    </main>
  </div>
</template>
