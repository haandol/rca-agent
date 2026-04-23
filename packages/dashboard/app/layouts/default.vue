<template>
  <div class="min-h-screen bg-gray-950">
    <header class="border-b border-gray-800 bg-gray-900/80 backdrop-blur sticky top-0 z-50">
      <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 h-14 flex items-center gap-4">
        <NuxtLink to="/" class="flex items-center gap-2 text-white font-semibold text-lg">
          <UIcon name="i-lucide-activity" class="size-5 text-green-400" />
          RCA Dashboard
        </NuxtLink>
        <div class="flex-1" />
        <UBadge color="neutral" variant="subtle" size="sm">Local</UBadge>
      </div>
    </header>
    <main class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-6">
      <slot />
    </main>
  </div>
</template>
