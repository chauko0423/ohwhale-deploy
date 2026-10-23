#include "MediaStream.h"
#include "FrameGenerator.h"
#include <algorithm>

namespace
{
    struct Format
    {
        GUID subtype;
        UINT32 width;
        UINT32 height;
    };

    // Offered formats, preferred first. NV12 is what Frame Server handles best. The sizes cover
    // everything the Android emulator requests, so it never has to rescale (rescaling to a
    // different aspect ratio adds black bars).
    const Format kFormats[] = {
        // 只给 16:9 横屏：QQ 是横屏容器，竖屏流会被它自己加边，反而更小。
        // 画面内容按这个比例构图。
        {MFVideoFormat_NV12, 1280, 720},
        {MFVideoFormat_NV12, 1920, 1080},
        {MFVideoFormat_RGB32, 1280, 720},
        {MFVideoFormat_RGB32, 1920, 1080},
    };

    HRESULT CreateVideoType(const Format& format, IMFMediaType** type)
    {
        const bool isRgb = format.subtype == MFVideoFormat_RGB32;
        const UINT32 stride = isRgb ? format.width * 4 : format.width;
        const UINT32 sampleSize = isRgb ? stride * format.height : format.width * format.height * 3 / 2;

        ComPtr<IMFMediaType> mediaType;
        RETURN_IF_FAILED(MFCreateMediaType(&mediaType));
        RETURN_IF_FAILED(mediaType->SetGUID(MF_MT_MAJOR_TYPE, MFMediaType_Video));
        RETURN_IF_FAILED(mediaType->SetGUID(MF_MT_SUBTYPE, format.subtype));
        RETURN_IF_FAILED(MFSetAttributeSize(mediaType.Get(), MF_MT_FRAME_SIZE, format.width, format.height));
        RETURN_IF_FAILED(MFSetAttributeRatio(mediaType.Get(), MF_MT_FRAME_RATE, kFrameRate, 1));
        RETURN_IF_FAILED(MFSetAttributeRatio(mediaType.Get(), MF_MT_PIXEL_ASPECT_RATIO, 1, 1));
        RETURN_IF_FAILED(mediaType->SetUINT32(MF_MT_INTERLACE_MODE, MFVideoInterlace_Progressive));
        RETURN_IF_FAILED(mediaType->SetUINT32(MF_MT_ALL_SAMPLES_INDEPENDENT, TRUE));
        RETURN_IF_FAILED(mediaType->SetUINT32(MF_MT_FIXED_SIZE_SAMPLES, TRUE));
        RETURN_IF_FAILED(mediaType->SetUINT32(MF_MT_DEFAULT_STRIDE, stride));
        RETURN_IF_FAILED(mediaType->SetUINT32(MF_MT_SAMPLE_SIZE, sampleSize));
        *type = mediaType.Detach();
        return S_OK;
    }

    HRESULT CreateSample(const std::vector<BYTE>& frame, IUnknown* token, IMFSample** result)
    {
        ComPtr<IMFMediaBuffer> buffer;
        RETURN_IF_FAILED(MFCreateMemoryBuffer(static_cast<DWORD>(frame.size()), &buffer));
        BYTE* data = nullptr;
        RETURN_IF_FAILED(buffer->Lock(&data, nullptr, nullptr));
        memcpy(data, frame.data(), frame.size());
        buffer->Unlock();
        RETURN_IF_FAILED(buffer->SetCurrentLength(static_cast<DWORD>(frame.size())));

        ComPtr<IMFSample> sample;
        RETURN_IF_FAILED(MFCreateSample(&sample));
        RETURN_IF_FAILED(sample->AddBuffer(buffer.Get()));
        RETURN_IF_FAILED(sample->SetSampleTime(MFGetSystemTime()));
        RETURN_IF_FAILED(sample->SetSampleDuration(kFrameDuration));
        if (token) RETURN_IF_FAILED(sample->SetUnknown(MFSampleExtension_Token, token));
        *result = sample.Detach();
        return S_OK;
    }
}

HRESULT MediaStream::RuntimeClassInitialize(IMFMediaSource* source, DWORD streamId)
{
    m_source = source;
    RETURN_IF_FAILED(MFCreateEventQueue(&m_queue));

    ComPtr<IMFMediaType> types[ARRAYSIZE(kFormats)];
    IMFMediaType* rawTypes[ARRAYSIZE(kFormats)];
    for (size_t i = 0; i < ARRAYSIZE(kFormats); ++i)
    {
        RETURN_IF_FAILED(CreateVideoType(kFormats[i], &types[i]));
        rawTypes[i] = types[i].Get();
    }
    RETURN_IF_FAILED(MFCreateStreamDescriptor(streamId, ARRAYSIZE(rawTypes), rawTypes, &m_descriptor));

    ComPtr<IMFMediaTypeHandler> handler;
    RETURN_IF_FAILED(m_descriptor->GetMediaTypeHandler(&handler));
    RETURN_IF_FAILED(handler->SetCurrentMediaType(rawTypes[0]));

    // Attributes Frame Server requires to expose the stream as a shared color camera.
    RETURN_IF_FAILED(m_descriptor->SetGUID(MF_DEVICESTREAM_STREAM_CATEGORY, PINNAME_VIDEO_CAPTURE));
    RETURN_IF_FAILED(m_descriptor->SetUINT32(MF_DEVICESTREAM_STREAM_ID, streamId));
    RETURN_IF_FAILED(m_descriptor->SetUINT32(MF_DEVICESTREAM_FRAMESERVER_SHARED, 1));
    RETURN_IF_FAILED(m_descriptor->SetUINT32(MF_DEVICESTREAM_ATTRIBUTE_FRAMESOURCE_TYPES, MFFrameSourceTypes_Color));
    return S_OK;
}

MediaStream::~MediaStream()
{
    StopWorker();
}

// IMFMediaEventGenerator ---------------------------------------------------------------------

IFACEMETHODIMP MediaStream::BeginGetEvent(IMFAsyncCallback* callback, IUnknown* state)
{
    return m_queue->BeginGetEvent(callback, state);
}

IFACEMETHODIMP MediaStream::EndGetEvent(IMFAsyncResult* result, IMFMediaEvent** event)
{
    return m_queue->EndGetEvent(result, event);
}

IFACEMETHODIMP MediaStream::GetEvent(DWORD flags, IMFMediaEvent** event)
{
    return m_queue->GetEvent(flags, event);
}

IFACEMETHODIMP MediaStream::QueueEvent(MediaEventType type, REFGUID extendedType, HRESULT status, const PROPVARIANT* eventValue)
{
    return m_queue->QueueEventParamVar(type, extendedType, status, eventValue);
}

// IMFMediaStream -----------------------------------------------------------------------------

IFACEMETHODIMP MediaStream::GetMediaSource(IMFMediaSource** source)
{
    if (!source) return E_POINTER;
    std::lock_guard lock(m_mutex);
    if (m_shutdown) return MF_E_SHUTDOWN;
    return m_source.CopyTo(source);
}

IFACEMETHODIMP MediaStream::GetStreamDescriptor(IMFStreamDescriptor** descriptor)
{
    if (!descriptor) return E_POINTER;
    std::lock_guard lock(m_mutex);
    if (m_shutdown) return MF_E_SHUTDOWN;
    return m_descriptor.CopyTo(descriptor);
}

IFACEMETHODIMP MediaStream::RequestSample(IUnknown* token)
{
    {
        std::lock_guard lock(m_mutex);
        if (m_shutdown) return MF_E_SHUTDOWN;
        if (m_state != MF_STREAM_STATE_RUNNING) return MF_E_MEDIA_SOURCE_WRONGSTATE;
        m_tokens.emplace_back(token);
    }
    m_cv.notify_all();
    return S_OK;
}

// IMFMediaStream2 ----------------------------------------------------------------------------

IFACEMETHODIMP MediaStream::SetStreamState(MF_STREAM_STATE state)
{
    MF_STREAM_STATE current;
    {
        std::lock_guard lock(m_mutex);
        if (m_shutdown) return MF_E_SHUTDOWN;
        current = m_state;
        if (state == current) return S_OK;
        // Toggling between running and paused does not affect the worker.
        if (state == MF_STREAM_STATE_PAUSED || (state == MF_STREAM_STATE_RUNNING && current == MF_STREAM_STATE_PAUSED))
        {
            m_state = state;
            return S_OK;
        }
    }
    return state == MF_STREAM_STATE_RUNNING ? Start() : Stop();
}

IFACEMETHODIMP MediaStream::GetStreamState(MF_STREAM_STATE* state)
{
    if (!state) return E_POINTER;
    std::lock_guard lock(m_mutex);
    if (m_shutdown) return MF_E_SHUTDOWN;
    *state = m_state;
    return S_OK;
}

// IKsControl ---------------------------------------------------------------------------------

IFACEMETHODIMP_(NTSTATUS) MediaStream::KsProperty(PKSPROPERTY, ULONG, PVOID, ULONG, ULONG*)
{
    return KS_NOT_SUPPORTED;
}

IFACEMETHODIMP_(NTSTATUS) MediaStream::KsMethod(PKSMETHOD, ULONG, PVOID, ULONG, ULONG*)
{
    return KS_NOT_SUPPORTED;
}

IFACEMETHODIMP_(NTSTATUS) MediaStream::KsEvent(PKSEVENT, ULONG, PVOID, ULONG, ULONG*)
{
    return KS_NOT_SUPPORTED;
}

// Control ------------------------------------------------------------------------------------

HRESULT MediaStream::Start()
{
    StopWorker();

    std::lock_guard lock(m_mutex);
    if (m_shutdown) return MF_E_SHUTDOWN;

    ComPtr<IMFMediaTypeHandler> handler;
    RETURN_IF_FAILED(m_descriptor->GetMediaTypeHandler(&handler));
    ComPtr<IMFMediaType> type;
    RETURN_IF_FAILED(handler->GetCurrentMediaType(&type));
    RETURN_IF_FAILED(type->GetGUID(MF_MT_SUBTYPE, &m_subtype));
    RETURN_IF_FAILED(MFGetAttributeSize(type.Get(), MF_MT_FRAME_SIZE, &m_width, &m_height));
    Trace(L"Stream starting: %ux%u %s", m_width, m_height, m_subtype == MFVideoFormat_NV12 ? L"NV12" : L"RGB32");

    m_state = MF_STREAM_STATE_RUNNING;
    m_stopWorker = false;
    m_worker = std::thread(&MediaStream::WorkerLoop, this);
    return m_queue->QueueEventParamVar(MEStreamStarted, GUID_NULL, S_OK, nullptr);
}

HRESULT MediaStream::Stop()
{
    StopWorker();

    std::lock_guard lock(m_mutex);
    if (m_shutdown) return MF_E_SHUTDOWN;
    m_state = MF_STREAM_STATE_STOPPED;
    return m_queue->QueueEventParamVar(MEStreamStopped, GUID_NULL, S_OK, nullptr);
}

void MediaStream::Shutdown()
{
    StopWorker();

    std::lock_guard lock(m_mutex);
    if (m_shutdown) return;
    m_shutdown = true;
    m_state = MF_STREAM_STATE_STOPPED;
    m_queue->Shutdown();
    m_source.Reset();
}

void MediaStream::StopWorker()
{
    std::thread worker;
    {
        std::lock_guard lock(m_mutex);
        m_stopWorker = true;
        m_tokens.clear();
        worker = std::move(m_worker);
    }
    m_cv.notify_all();
    if (worker.joinable()) worker.join();
}

void MediaStream::WorkerLoop()
{
    const HRESULT coInit = CoInitializeEx(nullptr, COINIT_MULTITHREADED);
    {
        // Format fields were written before this thread was created.
        FrameGenerator generator(m_subtype, m_width, m_height);
        LONGLONG nextFrameTime = 0;

        for (;;)
        {
            ComPtr<IUnknown> token;
            {
                std::unique_lock lock(m_mutex);
                m_cv.wait(lock, [this] { return m_stopWorker || !m_tokens.empty(); });
                if (m_stopWorker) break;

                // Pace delivery to the nominal frame rate.
                const LONGLONG now = MFGetSystemTime();
                if (now < nextFrameTime &&
                    m_cv.wait_for(lock, std::chrono::microseconds((nextFrameTime - now) / 10), [this] { return m_stopWorker; }))
                {
                    break;
                }
                if (m_tokens.empty()) continue;
                token = std::move(m_tokens.front());
                m_tokens.pop_front();
            }
            nextFrameTime = std::max(nextFrameTime + kFrameDuration, MFGetSystemTime());

            ComPtr<IMFSample> sample;
            HRESULT hr = CreateSample(generator.CurrentFrame(), token.Get(), &sample);
            if (SUCCEEDED(hr)) hr = m_queue->QueueEventParamUnk(MEMediaSample, GUID_NULL, S_OK, sample.Get());
            if (FAILED(hr) && hr != MF_E_SHUTDOWN) Trace(L"Failed to deliver sample: 0x%08X", hr);
        }
    }
    if (SUCCEEDED(coInit)) CoUninitialize();
}
