import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  absoluteUrl,
  cancelJob,
  connectWebSocket,
  downloadUrl,
  errorMessage,
  estimate,
  fetchBalance,
  fetchConfig,
  fetchHistory,
  fetchKeys,
  fetchRefs,
  refUrlToFile,
  setApiKeys,
  submitGeneration,
} from "./api";

const STAGE_LABEL = {
  queued: "В очереди",
  estimating: "Оценка",
  generating: "Генерация",
  saving: "Сохранение",
  done: "Готово",
  failed: "Ошибка",
  partial: "Частично",
};

// === Reuse settings: последняя конфигурация генерации в localStorage ===
const SETTINGS_KEY = "sami.lastSettings";

function loadLastSettings() {
  try {
    const raw = localStorage.getItem(SETTINGS_KEY);
    return raw ? JSON.parse(raw) : null;
  } catch {
    return null;
  }
}

function saveLastSettings(s) {
  try {
    // references (File) в localStorage не кладём — только текстовые настройки.
    const { references, ...rest } = s;
    localStorage.setItem(SETTINGS_KEY, JSON.stringify(rest));
  } catch {
    /* localStorage может быть недоступен — игнорируем */
  }
}

export default function App() {
  const [config, setConfig] = useState(null);
  const [balance, setBalance] = useState(null);
  const [prompt, setPrompt] = useState("");
  const [model, setModel] = useState("gpt");
  const [aspect, setAspect] = useState("1:1");
  const [sizeTier, setSizeTier] = useState("standard");
  const [quality, setQuality] = useState("medium");
  const [outputFormat, setOutputFormat] = useState("png");
  const [n, setN] = useState(1);
  const [references, setReferences] = useState([]); // массив File; порядок = @Image1..
  const [estimateData, setEstimateData] = useState(null);
  const [estimateLoading, setEstimateLoading] = useState(false);
  const [historyImages, setHistoryImages] = useState([]);
  const [historyJobs, setHistoryJobs] = useState([]);
  const [error, setError] = useState("");
  const [tab, setTab] = useState("generate"); // generate | history
  // Активные задачи: можно запускать несколько параллельно
  const [activeJobs, setActiveJobs] = useState({});
  // Экран подключения API-ключа
  const [keyInput, setKeyInput] = useState("");
  const [keyBusy, setKeyBusy] = useState(false);
  const [keyError, setKeyError] = useState("");
  const [keyOk, setKeyOk] = useState("");
  const [keysList, setKeysList] = useState([]); // masked-список пула: [{id,last_four,status}]
  // Просмотр картинки из истории
  const [viewer, setViewer] = useState(null); // {images, index}
  // Библиотека референсов
  const [refLibrary, setRefLibrary] = useState(null); // null — закрыта; {list, selected:Set, loading}
  const [refLibList, setRefLibList] = useState([]);
  const [dragOver, setDragOver] = useState(false);
  const wsRef = useRef(null);
  const estimateTimer = useRef(null);
  const dragCounter = useRef(0);

  // init
  useEffect(() => {
    fetchConfig()
      .then((cfg) => {
        setConfig(cfg);
        // Сначала дефолты из конфига…
        setModel(cfg.default_image_model || "gpt");
        setAspect(cfg.default_aspect || "1:1");
        setSizeTier(cfg.default_size_tier || "standard");
        setQuality(cfg.default_quality || "medium");
        setOutputFormat(cfg.default_format || "png");
        // …затем перекрываем последними сохранёнными настройками юзера (reuse).
        const last = loadLastSettings();
        if (last) {
          if (last.model) setModel(last.model);
          if (last.aspect) setAspect(last.aspect);
          if (last.sizeTier) setSizeTier(last.sizeTier);
          if (last.quality) setQuality(last.quality);
          if (last.outputFormat) setOutputFormat(last.outputFormat);
          if (last.prompt) setPrompt(last.prompt);
          if (last.n) setN(last.n);
        }
      })
      .catch((e) => setError(errorMessage(e)));
    refreshBalance();
    refreshHistory();
    refreshKeys();
  }, []);

  const refreshBalance = () => fetchBalance().then(setBalance).catch(() => {});
  const refreshHistory = () =>
    fetchHistory(60).then((h) => {
      setHistoryImages(h.images || []);
      setHistoryJobs(h.jobs || []);
    }).catch(() => {});

  // WebSocket
  useEffect(() => {
    wsRef.current = connectWebSocket((msg) => {
      setActiveJobs((prev) => {
        const jobId = msg.job_id;
        if (!prev[jobId]) return prev;
        const next = { ...prev };
        const current = next[jobId];
        next[jobId] = { ...current, progress: msg };
        if (msg.previews && msg.previews.length) {
          const set = new Set(current.results || []);
          msg.previews.forEach((p) => set.add(p));
          next[jobId] = { ...next[jobId], results: Array.from(set) };
        }
        if (["done", "failed", "partial", "cancelled"].includes(msg.stage)) {
          next[jobId] = { ...next[jobId], cancelling: false };
          refreshHistory();
          refreshBalance();   // показать изменившийся баланс
        }
        return next;
      });
    });
    return () => wsRef.current && wsRef.current.close();
  }, []);

  // live estimate
  useEffect(() => {
    if (!config) return;
    setEstimateLoading(true);
    clearTimeout(estimateTimer.current);
    estimateTimer.current = setTimeout(() => {
      estimate({ aspect, size_tier: sizeTier, quality, n, model })
        .then((d) => setEstimateData(d))
        .catch(() => setEstimateData(null))
        .finally(() => setEstimateLoading(false));
    }, 250);
    return () => clearTimeout(estimateTimer.current);
  }, [aspect, sizeTier, quality, n, model, config]);

  const maxN = useMemo(() => {
    if (!config) return 10;
    return config.max_n_per_call || 10;
  }, [config]);

  // Конфиг текущей модели (engine, supports_quality, label).
  const currentModel = useMemo(() => {
    const list = config?.image_models || [];
    return list.find((m) => m.key === model) || list[0] || { key: "gpt", engine: "openai", supports_quality: true, label: "GPT IMAGE 2" };
  }, [config, model]);

  const supportsQuality = currentModel?.supports_quality !== false;
  const maxSizeTier = currentModel?.max_size_tier || null; // null = без лимита
  const tierOrder = { standard: 0, "2k": 1, max: 2 };
  const tierDisabled = (t) => maxSizeTier && (tierOrder[t] || 0) > (tierOrder[maxSizeTier] || 0);

  // При переключении модели: если выбранный размер превышает лимит модели — понижаем.
  useEffect(() => {
    if (tierDisabled(sizeTier)) {
      setSizeTier(maxSizeTier || "standard");
    }
  }, [model, maxSizeTier]); // eslint-disable-line react-hooks/exhaustive-deps

  const handleGenerate = useCallback(async () => {
    setError("");
    if (!prompt.trim()) {
      setError("Введите промпт");
      return;
    }
    try {
      const res = await submitGeneration({ prompt, aspect, size_tier: sizeTier, quality, output_format: outputFormat, n, references, model });
      // Сохраняем настройки генерации для reuse (references не кладём — это File).
      saveLastSettings({ prompt, aspect, sizeTier, quality, outputFormat, n, model });
      setActiveJobs((prev) => {
        const idx = Object.keys(prev).length + 1;
        return {
          ...prev,
          [res.job_id]: { job: res, prompt, progress: null, results: [], cancelling: false, index: idx },
        };
      });
    } catch (e) {
      setError(errorMessage(e));
    }
  }, [prompt, aspect, sizeTier, quality, outputFormat, n, references, model]);

  const handleReuse = useCallback(() => {
    const last = loadLastSettings();
    if (!last) {
      setError("Нет сохранённых настроек — запустите генерацию хотя бы раз");
      return;
    }
    setError("");
    if (last.prompt) setPrompt(last.prompt);
    if (last.aspect) setAspect(last.aspect);
    if (last.sizeTier) setSizeTier(last.sizeTier);
    if (last.quality) setQuality(last.quality);
    if (last.outputFormat) setOutputFormat(last.outputFormat);
    if (last.model) setModel(last.model);
    if (last.n) setN(last.n);
    // references намеренно не восстанавливаем: File-объекты не сериализуются.
  }, []);

  const handleReuseFromImage = useCallback(async (img) => {
    if (!img) return;
    setError("");
    if (img.prompt) setPrompt(img.prompt);
    if (img.aspect) setAspect(img.aspect);
    if (img.size_tier) setSizeTier(img.size_tier);
    if (img.quality) setQuality(img.quality);
    if (img.output_format) setOutputFormat(img.output_format);
    if (img.model) setModel(img.model);
    // N по умолчанию оставляем текущий — юзер сам выберет, сколько хочет в этот раз.
    setViewer(null);          // закрыть просмотрщик
    setTab("generate");       // перейти к генерации

    // Подтягиваем референсы той картинки (fetch→File), в правильном порядке.
    const refs = img.references || [];
    if (refs.length > 0) {
      try {
        const sorted = [...refs].sort((a, b) => (a.position ?? 0) - (b.position ?? 0));
        const files = await Promise.all(
          sorted.map((r, i) => refUrlToFile(r.url, r.filename || `reference-${i + 1}.png`))
        );
        setReferences(files.slice(0, maxRefs));
      } catch (e) {
        setError("Не удалось подгрузить референсы: " + errorMessage(e));
      }
    }
  }, [maxRefs]);

  const maxRefs = config?.max_references || 16;

  const handleAddReferences = useCallback((files) => {
    const arr = Array.from(files || []).filter((f) => f.type.startsWith("image/"));
    setReferences((prev) => [...prev, ...arr].slice(0, maxRefs));
  }, [maxRefs]);

  // === Библиотека референсов ===
  const openRefLibrary = useCallback(async () => {
    setRefLibrary({ list: refLibList, selected: new Set(), loading: true });
    try {
      const data = await fetchRefs();
      setRefLibList(data.refs || []);
      setRefLibrary({ list: data.refs || [], selected: new Set(), loading: false });
    } catch (e) {
      setError("Не удалось загрузить библиотеку: " + errorMessage(e));
      setRefLibrary(null);
    }
  }, [refLibList]);

  const toggleRefSelect = useCallback((assetId) => {
    setRefLibrary((prev) => {
      if (!prev) return prev;
      const next = new Set(prev.selected);
      if (next.has(assetId)) next.delete(assetId);
      else next.add(assetId);
      return { ...prev, selected: next };
    });
  }, []);

  const addSelectedRefsToPrompt = useCallback(async () => {
    if (!refLibrary || refLibrary.selected.size === 0) {
      setRefLibrary(null);
      return;
    }
    const selectedIds = Array.from(refLibrary.selected);
    try {
      const files = await Promise.all(
        selectedIds.map((id) => {
          const r = refLibrary.list.find((x) => x.id === id);
          return refUrlToFile(r.url, r.filename || `reference-${id}.png`);
        })
      );
      setReferences((prev) => [...prev, ...files].slice(0, maxRefs));
      setRefLibrary(null);
    } catch (e) {
      setError("Не удалось добавить референсы: " + errorMessage(e));
    }
  }, [refLibrary, maxRefs]);

  const handleDragEnter = useCallback((e) => {
    e.preventDefault();
    e.stopPropagation();
    dragCounter.current += 1;
    if (e.dataTransfer.items && e.dataTransfer.items.length > 0) {
      setDragOver(true);
    }
  }, []);

  const handleDragLeave = useCallback((e) => {
    e.preventDefault();
    e.stopPropagation();
    dragCounter.current -= 1;
    if (dragCounter.current <= 0) {
      dragCounter.current = 0;
      setDragOver(false);
    }
  }, []);

  const handleDragOver = useCallback((e) => {
    e.preventDefault();
    e.stopPropagation();
  }, []);

  const handleDrop = useCallback((e) => {
    e.preventDefault();
    e.stopPropagation();
    dragCounter.current = 0;
    setDragOver(false);
    if (e.dataTransfer.files && e.dataTransfer.files.length > 0) {
      handleAddReferences(e.dataTransfer.files);
    }
  }, [handleAddReferences]);

  const handleRemoveReference = useCallback((index) => {
    setReferences((prev) => prev.filter((_, i) => i !== index));
  }, []);

  const handleCancel = useCallback(async (jobId) => {
    if (!jobId) return;
    setActiveJobs((prev) => {
      if (!prev[jobId]) return prev;
      return { ...prev, [jobId]: { ...prev[jobId], cancelling: true } };
    });
    try {
      await cancelJob(jobId);
    } catch (e) {
      setError(errorMessage(e));
      setActiveJobs((prev) => {
        if (!prev[jobId]) return prev;
        return { ...prev, [jobId]: { ...prev[jobId], cancelling: false } };
      });
    }
  }, []);

  const handleClearFinished = useCallback(() => {
    setActiveJobs((prev) => {
      const next = {};
      for (const [jobId, data] of Object.entries(prev)) {
        const stage = data.progress?.stage;
        if (!stage || !["done", "failed", "partial", "cancelled"].includes(stage)) {
          next[jobId] = data;
        }
      }
      return next;
    });
  }, []);

  const refreshKeys = useCallback(() => {
    fetchKeys()
      .then((data) => setKeysList(data.keys || []))
      .catch(() => setKeysList([]));
  }, []);

  const handleSetKey = useCallback(async () => {
    setKeyError("");
    setKeyOk("");
    const raw = keyInput.trim();
    if (!raw) {
      setKeyError("Вставьте хотя бы один ключ");
      return;
    }
    const lines = raw.split(/\r?\n/).map((s) => s.trim()).filter(Boolean);
    if (lines.some((k) => k.length < 16)) {
      setKeyError("Один из ключей выглядит слишком коротким");
      return;
    }
    setKeyBusy(true);
    try {
      const res = await setApiKeys(raw);
      const okParts = [`✅ Подключено ключей: ${res.count} (активны: ${res.active_count})`];
      if (res.balance) okParts.push(res.balance);
      if (res.invalid && res.invalid.length) {
        okParts.push("Не прошли: " + res.invalid.map((i) => `…${i.key_tail}`).join(", "));
      }
      setKeyOk(okParts.join("\n"));
      setKeyInput("");
      setKeysList(res.pool || []);
      refreshBalance();
    } catch (e) {
      setKeyError(errorMessage(e));
    } finally {
      setKeyBusy(false);
    }
  }, [keyInput, refreshBalance]);

  const hasKey = Boolean(balance?.has_key);

  // навигация в просмотрщике
  const goViewer = useCallback((delta) => {
    setViewer((v) => {
      if (!v) return v;
      const len = v.images.length;
      return { ...v, index: (v.index + delta + len) % len };
    });
  }, []);

  //Esc/стрелки для просмотрщика
  useEffect(() => {
    if (!viewer) return;
    const onKey = (e) => {
      if (e.key === "Escape") setViewer(null);
      else if (e.key === "ArrowRight") goViewer(1);
      else if (e.key === "ArrowLeft") goViewer(-1);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [viewer, goViewer]);

  return (
    <div
      className={"app-shell" + (dragOver ? " drag-over" : "")}
      onDragEnter={handleDragEnter}
      onDragOver={handleDragOver}
      onDragLeave={handleDragLeave}
      onDrop={handleDrop}
    >
      {dragOver && (
        <div className="drop-overlay">
          <span>📎 Отпустите фото, чтобы добавить референс</span>
        </div>
      )}
      <TopBar balance={balance} config={config} />

      <div className="tabs">
        <button className={tab === "generate" ? "tab active" : "tab"} onClick={() => setTab("generate")}>Генерация</button>
        <button className={tab === "keys" ? "tab active" : "tab"} onClick={() => setTab("keys")}>API-ключи</button>
        <button className={tab === "history" ? "tab active" : "tab"} onClick={() => setTab("history")}>История</button>
      </div>

      {error && <div className="alert danger">{error}</div>}

      {tab === "generate" && hasKey && (
        <PriceCheatSheet config={config} estimateData={estimateData} n={n} model={model} currentModel={currentModel} />
      )}

      {tab === "generate" && !hasKey && (
        <div className="panel">
          <h2>Подключите API-ключ</h2>
          <p className="hint">
            Чтобы генерировать картинки, подключите ключ AIGate. Можно несколько —
            для многопоточности и надёжности.
          </p>
          <button className="btn primary" onClick={() => setTab("keys")}>Перейти к API-ключам</button>
        </div>
      )}

      {tab === "keys" && (
        <KeyPanel
          keyInput={keyInput} setKeyInput={setKeyInput}
          keyBusy={keyBusy} keyError={keyError} keyOk={keyOk}
          keysList={keysList}
          onSubmit={handleSetKey}
        />
      )}

      {tab === "generate" && hasKey && (
        <div className="panel">
          <ModelSwitch
            models={config?.image_models || []}
            value={model}
            onChange={setModel}
          />
          <PromptField
            value={prompt}
            onChange={setPrompt}
            references={references}
            maxLen={config?.max_prompt_length || 16000}
          />

          <div className="field">
            <span className="field-label">Соотношение сторон</span>
            <div className="aspect-pills">
              {(config?.aspects || ["1:1", "9:16", "16:9", "4:3", "3:4", "3:2", "2:3"]).map((a) => (
                <button
                  key={a}
                  type="button"
                  className={"aspect-pill" + (aspect === a ? " active" : "")}
                  onClick={() => setAspect(a)}
                >
                  <span className={"aspect-rect ar-" + a.replace(":", "-")} />
                  {a}
                </button>
              ))}
            </div>
          </div>

          <div className="field">
            <span className="field-label">Размер (детализация)</span>
            <div className="tier-pills">
              {(config?.size_tiers || ["standard", "2k", "max"]).map((t) => (
                <button
                  key={t}
                  type="button"
                  className={"tier-pill" + (sizeTier === t ? " active" : "") + (tierDisabled(t) ? " disabled" : "")}
                  disabled={tierDisabled(t)}
                  onClick={() => !tierDisabled(t) && setSizeTier(t)}
                  title={tierDisabled(t) ? "Недоступно для этой модели" : ""}
                >
                  {t === "standard" ? "Стандарт" : t === "2k" ? "2K" : "Макс (4K)"}
                </button>
              ))}
            </div>
            <div className="hint">
              {maxSizeTier === "2k"
                ? "BANANA 2 поддерживает только Стандарт и 2K (4K недоступен для этой модели)."
                : "Стандарт — быстро и дёшево. 2K — больше деталей. Макс — максимум (до 3840px)."}
            </div>
          </div>

          <div className="field">
            <span className="field-label">
              Референсы (свои персонажи/фото) — {references.length}/{maxRefs}
            </span>
            <ReferenceUploader
              references={references}
              maxRefs={maxRefs}
              onAdd={handleAddReferences}
              onRemove={handleRemoveReference}
              onDragClear={() => {
                dragCounter.current = 0;
                setDragOver(false);
              }}
            />
            <div className="ref-actions">
              <button type="button" className="btn ghost small" onClick={openRefLibrary}>
                📚 Мои референсы
              </button>
              <span className="hint">
                Порядок = @Image1, @Image2… В промпте ссылайся: «@Image1 — главный герой, @Image2 — фон».
              </span>
            </div>
          </div>

          <div className="grid-2">
            <Selector label="Качество (quality)" value={quality} options={config?.qualities || []} onChange={setQuality} />
            <Selector
              label="Формат"
              value={outputFormat}
              options={config?.formats || []}
              onChange={setOutputFormat}
            />
          </div>
          <div className="hint format-hint">
            {outputFormat === "jpeg"
              ? "JPEG — меньше вес, но сжимает с потерями (мылит тонкие детали)"
              : outputFormat === "webp"
              ? "WebP — баланс размера и качества"
              : "PNG — без потерь, максимальная чёткость (больше вес)"}
          </div>
          <div className="field">
            <span className="field-label">Количество: {n}</span>
            <input
              type="range"
              min={1}
              max={maxN}
              value={Math.min(n, maxN)}
              onChange={(e) => setN(parseInt(e.target.value, 10))}
            />
            <div className="hint">До {maxN} за запуск.</div>
          </div>

          <EstimateBar data={estimateData} loading={estimateLoading} />

          <div className="btn-row">
            <button className="btn primary" disabled={!prompt.trim()} onClick={handleGenerate}>
              Сгенерировать
            </button>
            <button className="btn ghost" onClick={handleReuse} title="Подгрузить промпт и настройки последней генерации">
              ♻ Reuse
            </button>
            {Object.values(activeJobs).some((d) => ["done", "failed", "partial", "cancelled"].includes(d.progress?.stage)) && (
              <button className="btn ghost" onClick={handleClearFinished}>
                Очистить завершённые
              </button>
            )}
          </div>

          {Object.entries(activeJobs).map(([jobId, data]) => (
            <JobProgress
              key={jobId}
              jobId={jobId}
              data={data}
              onCancel={handleCancel}
            />
          ))}
        </div>
      )}

      {tab === "history" && (
        <HistoryPanel images={historyImages} jobs={historyJobs} onOpen={setViewer} />
      )}

      {viewer && (
        <Viewer
          images={viewer.images}
          index={viewer.index}
          onClose={() => setViewer(null)}
          onNav={goViewer}
          onReuse={handleReuseFromImage}
        />
      )}

      {refLibrary && (
        <RefLibraryModal
          list={refLibrary.list}
          selected={refLibrary.selected}
          loading={refLibrary.loading}
          onToggle={toggleRefSelect}
          onAdd={addSelectedRefsToPrompt}
          onClose={() => setRefLibrary(null)}
        />
      )}
    </div>
  );
}

function ReferenceUploader({ references, maxRefs, onAdd, onRemove, onDragClear }) {
  const inputRef = useRef(null);
  const [previews, setPreviews] = useState([]);

  // генерируем object URLs для превью
  useEffect(() => {
    const urls = references.map((f) => URL.createObjectURL(f));
    setPreviews(urls);
    return () => urls.forEach((u) => URL.revokeObjectURL(u));
  }, [references]);

  const handleUploaderDrop = (e) => {
    e.preventDefault();
    e.stopPropagation();
    if (e.dataTransfer.files && e.dataTransfer.files.length > 0) {
      onAdd(e.dataTransfer.files);
    }
    onDragClear?.();
  };

  const handleUploaderDragOver = (e) => {
    e.preventDefault();
    e.stopPropagation();
  };

  const handleUploaderDragLeave = (e) => {
    e.preventDefault();
    e.stopPropagation();
    onDragClear?.();
  };

  return (
    <div className="ref-uploader" onDrop={handleUploaderDrop} onDragOver={handleUploaderDragOver} onDragLeave={handleUploaderDragLeave}>
      {references.length === 0 && (
        <div className="ref-empty-zone">
          <span>📎 Перетащите фото сюда или нажмите «+»</span>
        </div>
      )}
      <div className="ref-grid">
        {previews.map((url, i) => (
          <div key={i} className="ref-cell">
            <img src={url} alt={`ref ${i + 1}`} />
            <span className="ref-badge">@Image{i + 1}</span>
            <button
              type="button"
              className="ref-remove"
              onClick={() => onRemove(i)}
              title="Удалить"
            >
              ✕
            </button>
          </div>
        ))}
        <button
          type="button"
          className="ref-add"
          onClick={() => inputRef.current?.click()}
          disabled={references.length >= maxRefs}
        >
          +<br />
          <span className="hint">добавить</span>
        </button>
      </div>
      <input
        ref={inputRef}
        type="file"
        accept="image/*"
        multiple
        style={{ display: "none" }}
        onChange={(e) => {
          onAdd(e.target.files);
          e.target.value = "";
        }}
      />
    </div>
  );
}

function ResultsGrid({ total, ready }) {
  // total — сколько всего ждём; ready — массив готовых URL.
  // Готовые ячейки показывают фото, остальные — skeleton-плейсхолдер.
  const cells = [];
  for (let i = 0; i < total; i++) {
    const url = ready[i];
    if (url) {
      cells.push(
        <a key={i} className="result-cell" href={absoluteUrl(url)} target="_blank" rel="noreferrer">
          <img src={absoluteUrl(url)} alt={`result ${i + 1}`} />
        </a>
      );
    } else {
      cells.push(
        <div key={i} className="result-cell skeleton">
          <span className="skeleton-num">{i + 1}</span>
          <span className="skeleton-spinner" />
        </div>
      );
    }
  }
  return <div className="results-grid">{cells}</div>;
}

function PromptField({ value, onChange, references, maxLen }) {
  const ref = useRef(null);
  const [showMenu, setShowMenu] = useState(false);
  const [previews, setPreviews] = useState([]);

  useEffect(() => {
    const urls = references.map((f) => URL.createObjectURL(f));
    setPreviews(urls);
    return () => urls.forEach((u) => URL.revokeObjectURL(u));
  }, [references]);

  const detect = () => {
    const el = ref.current;
    if (!el) return;
    const pos = el.selectionStart;
    const before = value.slice(0, pos);
    // Триггер: последний символ @, перед ним пробел или начало строки.
    if (references.length > 0 && /(^|\s)@$/.test(before)) {
      setShowMenu(true);
    } else {
      setShowMenu(false);
    }
  };

  const insert = (n) => {
    const el = ref.current;
    if (!el) return;
    const pos = el.selectionStart;
    const before = value.slice(0, pos).replace(/@$/, `@Image${n} `);
    const after = value.slice(pos);
    const next = before + after;
    onChange(next);
    setShowMenu(false);
    setTimeout(() => {
      el.focus();
      const newPos = before.length;
      el.setSelectionRange(newPos, newPos);
    }, 0);
  };

  return (
    <label className="field">
      <span className="field-label">Промпт</span>
      <div className="prompt-wrap">
        <textarea
          ref={ref}
          className="input textarea"
          placeholder="Опиши картинку… можно подробно: стиль, освещение, композиция. Напиши @ чтобы сослаться на референс."
          value={value}
          onChange={(e) => { onChange(e.target.value); setTimeout(detect, 0); }}
          onKeyUp={detect}
          onClick={detect}
          onInput={detect}
          rows={5}
          maxLength={maxLen}
        />
        {showMenu && (
          <div className="at-menu">
            <div className="at-menu-title">Референсы — кликни чтобы вставить</div>
            <div className="at-menu-list">
              {references.map((_, i) => (
                <button
                  key={i}
                  type="button"
                  className="at-item"
                  onClick={() => insert(i + 1)}
                >
                  {previews[i] && <img src={previews[i]} alt="" />}
                  <span>@Image{i + 1}</span>
                </button>
              ))}
            </div>
          </div>
        )}
      </div>
      <div className="prompt-meta">
        <span className="hint">{value.length} / {maxLen}</span>
        <span className="hint">{references.length > 0 ? "Напиши @ чтобы вставить референс" : "длинный промпт — детальнее результат"}</span>
      </div>
    </label>
  );
}

function PriceCheatSheet({ config, estimateData, n, model, currentModel }) {
  const isBanana = (currentModel?.engine === "gemini");
  const pp = config?.price_per_image || { low: 0.0015, medium: 0.009, high: 0.012 };
  const rub = config?.usd_to_rub || 92;
  const toRub = (usd) => usd * rub;
  // Живой просчёт под текущий выбор
  const liveRub = estimateData?.total_rub;
  const liveUsd = estimateData?.total;
  const liveTokens = estimateData?.tokens_estimated;

  if (isBanana) {
    const gp = config?.gemini_price_by_size || { "1K": 0.00902, "2K": 0.01353 };
    const sizes = ["1K", "2K"];   // 4K не поддерживается flash-image — не показываем
    return (
      <div className="cheatsheet">
        <div className="cs-title">BANANA 2 — цена за 1 картинку по размеру</div>
        <div className="cs-row">
          {sizes.map((s) => (
            <div key={s} className="cs-cell">
              <span className="cs-q">{s}</span>
              <span className="cs-price">{toRub(gp[s]).toFixed(2)} ₽</span>
              <span className="cs-usd">${gp[s].toFixed(4)}</span>
            </div>
          ))}
        </div>
        <div className="cs-live">
          {liveRub != null ? (
            <>Сейчас ({n} шт): <b>≈ {liveRub.toFixed(2)} ₽</b> <span className="muted">(${(liveUsd || 0).toFixed(4)})</span></>
          ) : (
            <span className="muted">Выбери параметры — покажу цену</span>
          )}
          <span className="cs-note"> · промпт почти не влияет на цену — её задаёт размер</span>
        </div>
      </div>
    );
  }

  return (
    <div className="cheatsheet">
      <div className="cs-title">Цены за 1 картинку (стандартный размер)</div>
      <div className="cs-row">
        {["low", "medium", "high"].map((q) => (
          <div key={q} className="cs-cell">
            <span className="cs-q">{q}</span>
            <span className="cs-price">{toRub(pp[q]).toFixed(2)} ₽</span>
            <span className="cs-usd">${pp[q].toFixed(4)}</span>
          </div>
        ))}
      </div>
      <div className="cs-live">
        {liveRub != null ? (
          <>Сейчас ({n} шт): <b>≈ {liveRub.toFixed(2)} ₽</b> <span className="muted">(${(liveUsd || 0).toFixed(4)})</span></>
        ) : (
          <span className="muted">Выбери параметры — покажу цену</span>
        )}
        <span className="cs-note"> · точная цена зависит от сложности промпта и размера — финальный расход по балансу</span>
      </div>
    </div>
  );
}

function ModelSwitch({ models, value, onChange }) {
  if (!models || models.length === 0) return null;
  return (
    <div className="field">
      <span className="field-label">Модель</span>
      <div className="model-pills">
        {models.map((m) => (
          <button
            key={m.key}
            type="button"
            className={"model-pill" + (value === m.key ? " active" : "")}
            onClick={() => onChange(m.key)}
          >
            {m.label}
          </button>
        ))}
      </div>
    </div>
  );
}

function TopBar({ balance, config }) {
  const hasKey = Boolean(balance?.has_key);
  const poolCount = balance?.pool?.count || 0;
  let balanceText = "нет ключа";
  if (hasKey) {
    const usd = balance?.balance_usd;
    const rub = balance?.balance_rub;
    if (usd != null && rub != null) {
      balanceText = `$${usd.toFixed(2)} · ${rub.toFixed(0)} ₽`;
    } else if (balance?.error) {
      balanceText = "ошибка ключа";
    } else {
      balanceText = "ключ ok";
    }
  }
  return (
    <div className="topbar">
      <div className="brand-mark avatar">🎨</div>
      <div className="brand-copy">
        <h1>
          sami studio
          <span className="version-badge">v7</span>
        </h1>
        <div className="eyebrow">генерация изображений</div>
      </div>
      <div className={"key-chip" + (hasKey ? " ready" : "")}>
        {poolCount > 1 && <span className="pool-badge" title={`Пул из ${poolCount} ключей — многопоточность`}>🔑{poolCount}</span>}
        {balanceText}
      </div>
    </div>
  );
}

function KeyPanel({ keyInput, setKeyInput, keyBusy, keyError, keyOk, keysList, onSubmit }) {
  const statusLabel = { ok: "активен", cooldown: "cooldown", dead: "мёртв" };
  const hasKeys = keysList && keysList.length > 0;
  return (
    <div className="panel">
      <div className="key-intro">
        <h2>{hasKeys ? "API-ключи" : "Подключите API-ключ(и)"}</h2>
        <p className="hint">
          {hasKeys
            ? "Добавьте ещё ключи — параллельные генерации распределятся по ним. "
            : "Генерации списываются с баланса вашего ключа. Можно добавить несколько — параллельные генерации распределятся по ним. "}
          Зарегистрируйтесь на <a href="https://aigate.shop" target="_blank" rel="noreferrer">aigate.shop</a>,
          пополните баланс и скопируйте API-ключ(и) из кабинета.
        </p>
      </div>
      {keyError && <div className="alert danger">{keyError}</div>}
      {keyOk && <div className="alert success">{keyOk}</div>}
      <label className="field">
        <span className="field-label">{hasKeys ? "Добавить ключи — по одному на строку" : "API-ключи — по одному на строку"}</span>
        <textarea
          className="input textarea"
          placeholder={"sk-...\nsk-...\nsk-..."}
          value={keyInput}
          onChange={(e) => setKeyInput(e.target.value)}
          autoComplete="off"
          rows={5}
        />
      </label>
      {keysList && keysList.length > 0 && (
        <div className="key-pool-list">
          <span className="field-label">Пул ключей ({keysList.length})</span>
          {keysList.map((k) => (
            <div key={k.id} className={"key-row key-" + (k.status || "ok")}>
              <span className="key-tail">••••{k.last_four}</span>
              <span className={"key-badge " + (k.status || "ok")}>{statusLabel[k.status] || k.status}</span>
            </div>
          ))}
        </div>
      )}
      <button className="btn primary" disabled={keyBusy || keyInput.trim().length < 16} onClick={onSubmit}>
        {keyBusy ? "Проверяю…" : "Подключить"}
      </button>
    </div>
  );
}

function Selector({ label, value, options, onChange, optionLabel }) {
  return (
    <label className="field">
      <span className="field-label">{label}</span>
      <select className="input" value={value} onChange={(e) => onChange(e.target.value)}>
        {options.map((opt) => (
          <option key={opt} value={opt}>{optionLabel ? optionLabel(opt) : opt}</option>
        ))}
      </select>
    </label>
  );
}

function EstimateBar({ data, loading }) {
  if (loading) return <div className="estimate-bar">Считаем цену…</div>;
  if (!data) return null;
  const rub = data.total_rub != null ? `${data.total_rub.toFixed(2)} ₽` : `$${(data.total || 0).toFixed(4)}`;
  const tokens = data.tokens_estimated != null ? ` · ~${data.tokens_estimated} токенов` : "";
  return (
    <div className="estimate-bar">
      <span>≈ {rub}</span>
      <span className="muted">оценка{tokens}</span>
    </div>
  );
}

function HistoryPanel({ images, jobs, onOpen }) {
  if (!images || images.length === 0) {
    return (
      <div className="panel">
        <div className="hint">Пока нет сгенерированных картинок. Перейди во вкладку «Генерация».</div>
      </div>
    );
  }
  return (
    <div className="panel">
      <div className="hist-head">
        <span className="field-label">Все картинки ({images.length})</span>
        <span className="hint">клик — открыть</span>
      </div>
      <div className="gallery-grid">
        {images.map((img) => (
          <button
            key={img.id}
            className="gallery-cell"
            onClick={() => onOpen({ images, index: images.indexOf(img) })}
          >
            <img src={absoluteUrl(img.url)} alt={img.prompt || ""} loading="lazy" />
          </button>
        ))}
      </div>
    </div>
  );
}

function tierLabel(tier) {
  if (!tier) return "—";
  if (tier === "standard") return "Стандарт";
  if (tier === "2k") return "2K";
  if (tier === "max") return "4K";
  return tier;
}

function Viewer({ images, index, onClose, onNav, onReuse }) {
  const img = images[index];
  if (!img) return null;
  return (
    <div className="viewer" onClick={onClose}>
      <div className="viewer-bar" onClick={(e) => e.stopPropagation()}>
        <span className="viewer-info">{index + 1} / {images.length}</span>
        <button
          className="btn small ghost"
          onClick={(e) => { e.stopPropagation(); onReuse && onReuse(img); }}
          title="Подгрузить настройки этой картинки в генератор"
        >
          ♻ Reuse
        </button>
        <button
          className="btn small"
          onClick={(e) => {
            e.stopPropagation();
            const filename = (img.url && img.url.split("/").pop()) || `image-${index + 1}.png`;
            downloadUrl(img.url, filename).catch((err) => {
              console.error("Download failed", err);
              // последний fallback — открыть в новой вкладке
              window.open(absoluteUrl(img.url), "_blank");
            });
          }}
        >
          ↓ Скачать
        </button>
        <button className="btn small" onClick={onClose}>✕</button>
      </div>
      <div className="viewer-stage" onClick={(e) => e.stopPropagation()}>
        <button className="nav-btn" onClick={() => onNav(-1)}>‹</button>
        <img src={absoluteUrl(img.url)} alt={img.prompt || ""} />
        <button className="nav-btn" onClick={() => onNav(1)}>›</button>
      </div>
      {img.prompt && (
        <div className="viewer-caption" onClick={(e) => e.stopPropagation()}>
          <span className="muted">
            {(img.model === "banana" ? "BANANA 2" : "GPT IMAGE 2")}
            {" · "}{img.aspect || "—"}
            {" · "}{tierLabel(img.size_tier)}
            {" · "}{img.quality || "—"}
            {img.output_format ? ` · ${img.output_format}` : ""}
            {img.cost_real != null ? ` · $${img.cost_real.toFixed(4)}` : ""}
            {img.n ? ` · ${img.n} шт` : ""}
          </span>
          <div className="viewer-prompt">{img.prompt}</div>
        </div>
      )}
    </div>
  );
}

function RefLibraryModal({ list, selected, loading, onToggle, onAdd, onClose }) {
  return (
    <div className="ref-library" onClick={onClose}>
      <div className="ref-lib-panel" onClick={(e) => e.stopPropagation()}>
        <div className="ref-lib-head">
          <span className="field-label">Мои референсы ({list.length})</span>
          <span className="hint">клик — выбрать, потом «Добавить»</span>
          <button className="btn small" onClick={onClose}>✕</button>
        </div>
        {loading ? (
          <div className="hint" style={{ padding: 24, textAlign: "center" }}>Загружаю…</div>
        ) : list.length === 0 ? (
          <div className="hint" style={{ padding: 24, textAlign: "center" }}>
            Библиотека пуста. Загрузите референсы при генерации — они сохранятся сюда.
          </div>
        ) : (
          <div className="ref-lib-grid">
            {list.map((r) => (
              <button
                key={r.id}
                type="button"
                className={"ref-lib-cell" + (selected.has(r.id) ? " selected" : "")}
                onClick={() => onToggle(r.id)}
              >
                <img src={absoluteUrl(r.url)} alt={r.filename || ""} loading="lazy" />
                {selected.has(r.id) && <span className="ref-lib-check">✓</span>}
              </button>
            ))}
          </div>
        )}
        <div className="btn-row" style={{ marginTop: 12 }}>
          <button className="btn primary" disabled={selected.size === 0} onClick={onAdd}>
            Добавить{selected.size > 0 ? ` (${selected.size})` : ""}
          </button>
        </div>
      </div>
    </div>
  );
}

function JobProgress({ jobId, data, onCancel }) {
  const { job, prompt, progress, results, cancelling, index } = data;
  const total = job?.n || progress?.total_count || results.length || 1;
  const done = progress?.done_count || results.length;
  const stage = progress?.stage || "queued";
  const isFinal = ["done", "failed", "partial", "cancelled"].includes(stage);
  const progressPct = progress?.progress ?? (results.length / total * 100);
  const shortPrompt = prompt ? prompt.slice(0, 35) + (prompt.length > 35 ? "…" : "") : "";

  return (
    <div className={"progress-block" + (isFinal ? "" : " is-active")}>
      <div className="progress-head">
        <span>
          <span className="job-number">#{index || 1}</span>
          {shortPrompt && <span className="job-prompt" title={prompt}>{shortPrompt}</span>}
        </span>
        <span>{STAGE_LABEL[stage] || stage} · {done}/{total}</span>
      </div>
      <div className="progress-bar">
        <div className="progress-fill" style={{ width: `${progressPct}%` }} />
      </div>
      <div className="hint">{progress?.message || "В очереди…"}</div>
      {!isFinal && (
        <div className="btn-row" style={{ marginTop: 8 }}>
          <button className="btn ghost" disabled={cancelling} onClick={() => onCancel(jobId)}>
            {cancelling ? "Отменяю…" : "Отменить"}
          </button>
        </div>
      )}
      {results.length > 0 && <ResultsGrid total={total} ready={results} />}
    </div>
  );
}
