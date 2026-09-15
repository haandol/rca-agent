<script setup lang="ts">
/** A reading aid extracted from the report; the full report remains authoritative. */
import type { CausalLink } from '~/utils/causalChain';

const props = defineProps<{
  links: CausalLink[];
  /** Shown as the head of the chain — the symptom that started it. */
  symptom?: string;
}>();

const hasChain = computed(() => props.links.length > 0);
</script>

<template>
  <section v-if="hasChain">
    <div class="flex flex-wrap items-baseline gap-3 mb-5">
      <h2 class="detail-section-title">원인 사슬 · 5 Whys</h2>
      <span class="detail-label">
        보고서에서 추출한 {{ links.length }}단계
      </span>
    </div>

    <ol>
      <li v-for="link in links" :key="link.index" class="chain-link">
        <span class="chain-dot" aria-hidden="true">{{ link.index }}</span>
        <p class="chain-question">{{ link.question }}?</p>
        <p class="chain-answer">{{ link.answer }}</p>
      </li>
    </ol>
  </section>
</template>
