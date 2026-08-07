<script setup lang="ts">
/**
 * The finding, read as one descent from symptom to fix.
 *
 * The search that produced it ran in parallel and discarded most of what it
 * tried, but none of that is the answer. The answer is a single chain: each
 * question's answer becomes the next question, and the last one names the thing
 * that has to change. Showing the parallel tree here would make the reader do
 * the collapsing the report already did.
 */
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
    <div class="flex items-baseline gap-3 mb-6">
      <h2 class="label-sm uppercase tracking-[0.1em] font-semibold">
        원인의 사슬
      </h2>
      <span class="text-[11px] text-base-content/62">
        {{ links.length }}단계로 좁혔습니다
      </span>
    </div>

    <ol>
      <li v-for="link in links" :key="link.index" class="chain-link">
        <span class="chain-dot" aria-hidden="true">{{ link.index }}</span>
        <p class="chain-question">{{ link.question }}?</p>
        <p class="chain-answer">{{ link.answer }}</p>
      </li>
    </ol>

    <!-- The last link is where a fix belongs, so the page says so once rather
         than decorating every link. -->
    <p class="text-[12px] text-base-content/65 mt-6 pl-8">
      마지막 단계가 재발을 막는 지점입니다.
    </p>
  </section>
</template>
