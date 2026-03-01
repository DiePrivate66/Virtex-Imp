const runtimeHost =
  typeof window !== 'undefined' && window.location?.hostname
    ? window.location.hostname
    : '127.0.0.1';

export const environment = {
  production: false,
  apiBaseUrl: `http://${runtimeHost}:8000/pedido/api`
};
