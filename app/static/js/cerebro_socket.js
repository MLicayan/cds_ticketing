(function() {
  window.getCerebroSocket = window.getCerebroSocket || function() {
    if (!window.io) return null;
    if (!window.cerebroSocket) {
      const options = window.CEREBRO_SOCKET_OPTIONS || {};
      const socket = io({
        transports: options.transports || ["websocket"],
        upgrade: options.upgrade === undefined ? false : Boolean(options.upgrade),
      });
      window.cerebroSocket = socket;
    }
    return window.cerebroSocket;
  };
})();
