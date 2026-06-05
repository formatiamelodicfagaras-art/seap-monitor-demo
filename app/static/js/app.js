// SEAP Monitor - Frontend JS

// HTMX event handlers
document.addEventListener('htmx:afterRequest', function(event) {
    // Show success message for scan trigger
    if (event.detail.pathInfo.requestPath === '/monitor/scan-now') {
        if (event.detail.successful) {
            showToast('Scanarea a fost pornită cu succes!', 'success');
        } else {
            showToast('Eroare la pornirea scanării.', 'error');
        }
    }
});

// Simple toast notification
function showToast(message, type) {
    const toast = document.createElement('div');
    toast.className = `fixed bottom-4 right-4 px-4 py-3 rounded-lg shadow-lg text-sm z-50 ${
        type === 'success' ? 'bg-green-600 text-white' :
        type === 'error' ? 'bg-red-600 text-white' :
        'bg-blue-600 text-white'
    }`;
    toast.textContent = message;
    document.body.appendChild(toast);

    setTimeout(() => {
        toast.style.opacity = '0';
        toast.style.transition = 'opacity 0.3s';
        setTimeout(() => toast.remove(), 300);
    }, 3000);
}

// Auto-refresh for pages with pending analyses
(function() {
    const pendingElements = document.querySelectorAll('[data-status="extracting"], [data-status="analyzing"], [data-status="pending"]');
    if (pendingElements.length > 0) {
        setTimeout(() => location.reload(), 10000);
    }
})();
