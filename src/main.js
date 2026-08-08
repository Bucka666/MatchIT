import { initCamera, resetCamera, stopCamera } from './camera.js';
import { runAuthenticityPipeline } from './authenticity.js';
import { normalizeBackType, getBackType } from './backtype.js';
import { renderAuthenticityOverlay, hideAuthResult, showAuthLoading, hideAuthLoading } from './ui.js';

const videoEl = document.getElementById('camera-stream');
const captureBtn = document.getElementById('capture-btn');
const closeBtn = document.getElementById('auth-close-btn');
const resetBtn = document.getElementById('scan-again-btn');
const backTypeSelect = document.getElementById('back-type-select');

let stream = null;

async function captureCardImageFromCamera() {
  if (!videoEl || !videoEl.videoWidth || !videoEl.videoHeight) {
    throw new Error('Camera is not ready.');
  }

  const canvas = document.createElement('canvas');
  canvas.width = videoEl.videoWidth;
  canvas.height = videoEl.videoHeight;
  const ctx = canvas.getContext('2d');
  if (!ctx) {
    throw new Error('Unable to capture image.');
  }

  ctx.drawImage(videoEl, 0, 0, canvas.width, canvas.height);

  return new Promise((resolve, reject) => {
    canvas.toBlob((blob) => {
      if (blob) {
        resolve(blob);
      } else {
        reject(new Error('Unable to create image blob.'));
      }
    }, 'image/jpeg', 0.95);
  });
}

async function runAuthFromCapture() {
  showAuthLoading();
  try {
    const imageBlob = await captureCardImageFromCamera();
    const backType = normalizeBackType(getBackType(backTypeSelect));
    const auth = await runAuthenticityPipeline(imageBlob);
    renderAuthenticityOverlay({
      ...auth,
      back_type: backType,
      back: backType,
    });
  } catch (err) {
    console.error('Authenticity pipeline failed', err);
    renderAuthenticityOverlay({
      status: 'unknown',
      confidence: 0.35,
      needsReview: true,
      reason: 'Authenticity review unavailable. Please try again.',
      set_name: 'Unknown',
      set_code: '-',
      region: 'Unknown',
      product_type: 'Unknown',
      back_type: normalizeBackType(getBackType(backTypeSelect)),
      warnings: ['Authenticity review unavailable'],
    });
  } finally {
    hideAuthLoading();
  }
}

captureBtn?.addEventListener('click', async () => {
  await runAuthFromCapture();
});

closeBtn?.addEventListener('click', () => {
  hideAuthResult();
});

resetBtn?.addEventListener('click', async () => {
  hideAuthResult();
  if (stream) {
    stopCamera(stream);
    stream = null;
  }
  stream = await resetCamera(videoEl, (err) => {
    console.warn('Camera unavailable', err);
  });
  await runAuthFromCapture();
});

backTypeSelect?.addEventListener('change', async () => {
  await runAuthFromCapture();
});

window.addEventListener('load', async () => {
  stream = await initCamera(videoEl, (err) => {
    console.warn('Camera unavailable', err);
  });
  await runAuthFromCapture();
});
