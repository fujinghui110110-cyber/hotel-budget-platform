(() => {
  const form = document.getElementById('summary-form');
  if (!form) return;
  document.getElementById('summary-history-toggle')?.addEventListener('change', (event) => {
    document.querySelector('.summary-table').classList.toggle('show-history', event.target.checked);
  });
  const issue = document.getElementById('summary-issue');
  const status = document.getElementById('summary-save-state');
  form.addEventListener('input', (event) => {
    if (event.target.matches('input[data-initial]')) {
      event.target.closest('tr').classList.toggle('summary-dirty', event.target.value !== event.target.dataset.initial);
    }
    if (issue) issue.disabled = true;
    status.textContent = '有未保存的修改，请先保存并计算';
  });
})();
