// CloudFront Function (viewer request) for the public API's distribution: records the visitor's IP
// address in x-viewer-ip, replacing any value the visitor sent, so the regional WAF can rate-limit
// per visitor rather than per CloudFront edge. See infra/modules/api-cdn/main.tf.
function handler(event) {
  var request = event.request;
  request.headers["x-viewer-ip"] = { value: event.viewer.ip };
  return request;
}
