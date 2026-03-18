declare global {
  interface Window {
    __BOSCO_API_BASE_URL__?: string;
  }
}

function resolveApiBaseUrl(): string {
  if (typeof window === 'undefined') {
    return 'https://web-production-0bfcb.up.railway.app/pedido/api';
  }

  const runtimeOverride = window.__BOSCO_API_BASE_URL__?.trim();
  if (runtimeOverride) {
    return runtimeOverride;
  }

  if (window.location.hostname.endsWith('.railway.app')) {
    return `${window.location.origin}/pedido/api`;
  }

  return 'https://web-production-0bfcb.up.railway.app/pedido/api';
}

export const environment = {
  production: true,
  apiBaseUrl: resolveApiBaseUrl()
};
