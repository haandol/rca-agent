<script setup lang="ts">
const props = defineProps<{ open: boolean; title: string }>();
const emit = defineEmits<{ close: [] }>();
const dialog = ref<HTMLDialogElement | null>(null);
let previousFocus: HTMLElement | null = null;
let scrollPosition = 0;
/**
 * Match the native dialog to page state without opening a second modal.
 * Capture focus and scroll only on entry so content switches keep the return point.
 */
function sync() {
  if (props.open && dialog.value && !dialog.value.open) {
    previousFocus = document.activeElement as HTMLElement | null;
    scrollPosition = window.scrollY;
    dialog.value.showModal();
  } else if (!props.open && dialog.value?.open) {
    dialog.value.close();
  }
}
/** Return the operator to the original control and scroll position after any close path. */
function closed() {
  previousFocus?.focus({ preventScroll: true });
  window.scrollTo({ top: scrollPosition, behavior: 'instant' });
  emit('close');
}
watch(() => props.open, sync, { flush: 'post' });
onMounted(sync);
onBeforeUnmount(() => {
  if (dialog.value?.open) dialog.value.close();
});
</script>

<template>
  <dialog
    ref="dialog"
    class="modal"
    aria-labelledby="detail-dialog-title"
    @close="closed"
  >
    <div class="modal-box w-[calc(100%-1rem)] max-w-6xl max-h-[94dvh] p-0">
      <header
        class="sticky top-0 z-20 flex items-center justify-between gap-3 border-b border-base-content/15 bg-base-100 p-4 sm:px-6"
      >
        <h2 id="detail-dialog-title" class="text-lg font-semibold">
          {{ title }}
        </h2>
        <button
          class="btn btn-ghost btn-sm shrink-0"
          aria-label="상세 닫기"
          @click="dialog?.close()"
        >
          닫기 ✕
        </button>
      </header>
      <div class="p-4 sm:p-6"><slot /></div>
    </div>
    <form method="dialog" class="modal-backdrop">
      <button aria-label="상세 닫기">닫기</button>
    </form>
  </dialog>
</template>
