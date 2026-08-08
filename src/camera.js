export async function initCamera(videoEl, onError) {
  if (!videoEl || !navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    onError && onError({ message: 'Camera API is unavailable on this browser.' });
    return null;
  }

  try {
    const stream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: 'environment' } });
    videoEl.srcObject = stream;
    await videoEl.play().catch(() => {});
    return stream;
  } catch (err) {
    onError && onError(err);
    return null;
  }
}

export async function resetCamera(videoEl, onError) {
  if (videoEl && videoEl.srcObject && videoEl.srcObject.getTracks) {
    videoEl.srcObject.getTracks().forEach((track) => track.stop());
  }
  if (videoEl) {
    videoEl.srcObject = null;
  }
  return initCamera(videoEl, onError);
}

export function stopCamera(stream) {
  if (stream && stream.getTracks) {
    stream.getTracks().forEach((track) => track.stop());
  }
}
