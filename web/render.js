import { Viewer, readJson } from './viewer.js';

const canvas = document.querySelector('canvas');
const viewer = new Viewer(canvas);
let active;
window.pugcRender = async ({ url, camera, preset = 'iso', ids, island, width, height }) => {
  const started = performance.now();
  if (url !== active) {
    await viewer.setManifest(await readJson(url), { fit: false });
    active = url;
  }
  if (viewer.viewport?.[0] !== width || viewer.viewport?.[1] !== height) {
    viewer.resize(width, height);
    canvas.style.width = `${width}px`;
    canvas.style.height = `${height}px`;
  }
  if (camera) viewer.useCamera(camera);
  else viewer.frame(preset, { ids, island });
  const stats = viewer.draw();
  viewer.renderer.getContext().finish();
  return { ...stats, first_frame_ms: performance.now() - started };
};
window.pugcClose = () => {
  viewer.clear();
  active = null;
  return viewer.draw();
};
window.pugcMeasure = async ({ preset = 'iso', frames = 30 }) => {
  const times = [];
  viewer.frame(preset);
  for (let index = 0; index < frames; index++) {
    await new Promise(resolve => requestAnimationFrame(resolve));
    const start = performance.now();
    const camera = viewer.navigationCamera;
    camera.position.sub(viewer.frameCenter).applyAxisAngle(camera.up, .02).add(viewer.frameCenter);
    camera.lookAt(viewer.frameCenter);
    viewer.draw();
    viewer.renderer.getContext().finish();
    times.push(performance.now() - start);
  }
  return times;
};
