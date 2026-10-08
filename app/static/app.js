/**
 * GeoMeasure Studio — Modern Interactive Geospatial Frontend
 */

(function () {
  'use strict';

  // Global State
  const state = {
    activeFile: null,
    measurementsData: null,
    featuresData: [],
    selectedFeatureIndex: null,
    currentFilter: 'ALL',
    searchQuery: '',
    map: null,
    geojsonLayer: null,
    currentBasemap: 'dark',
    basemapLayers: {},
    featureLayerMap: new Map(), // index -> L.Path or L.CircleMarker
  };

  // DOM Elements Cache
  const dom = {
    apiStatusPill: document.getElementById('api-status-pill'),
    apiStatusText: document.getElementById('api-status-text'),
    dropzone: document.getElementById('file-dropzone'),
    fileInput: document.getElementById('file-input'),
    browseBtn: document.getElementById('browse-btn'),
    uploadProgress: document.getElementById('upload-progress'),
    progressText: document.getElementById('progress-text'),
    activeFilePanel: document.getElementById('active-file-panel'),
    featuresPanel: document.getElementById('features-panel'),
    fileName: document.getElementById('file-name'),
    fileCrs: document.getElementById('file-crs'),
    fileStatus: document.getElementById('file-status'),
    valTotalArea: document.getElementById('val-total-area'),
    valAreaAlt: document.getElementById('val-area-alt'),
    valTotalLength: document.getElementById('val-total-length'),
    valLengthAlt: document.getElementById('val-length-alt'),
    featuresTotalCount: document.getElementById('features-total-count'),
    breakdownPills: document.getElementById('breakdown-pills'),
    featuresList: document.getElementById('features-list'),
    featureSearchInput: document.getElementById('feature-search-input'),
    btnFitBounds: document.getElementById('btn-fit-bounds'),
    btnBasemapToggle: document.getElementById('btn-basemap-toggle'),
    basemapDropdown: document.getElementById('basemap-dropdown'),
    currentBasemapLabel: document.getElementById('current-basemap-label'),
    mouseCoords: document.getElementById('mouse-coords'),
    featureDetailDrawer: document.getElementById('feature-detail-drawer'),
    detailCloseBtn: document.getElementById('detail-close-btn'),
    detailTitle: document.getElementById('detail-title'),
    detailSubtitle: document.getElementById('detail-subtitle'),
    detailGeomIcon: document.getElementById('detail-geom-icon'),
    detailStatusBadge: document.getElementById('detail-status-badge'),
    detailLayerText: document.getElementById('detail-layer-text'),
    detailMeasureGrid: document.getElementById('detail-measure-grid'),
    detailCrsMethod: document.getElementById('detail-crs-method'),
    detailGeodesicBlock: document.getElementById('detail-geodesic-block'),
    detailGeodesicCard: document.getElementById('detail-geodesic-card'),
    detailPropertiesTable: document.getElementById('detail-properties-table'),
    detailPropertiesBody: document.getElementById('detail-properties-body'),
    exportJsonBtn: document.getElementById('export-json-btn'),
    exportCsvBtn: document.getElementById('export-csv-btn'),
    deleteFileBtn: document.getElementById('delete-file-btn'),
    tabFilterAll: document.getElementById('tab-filter-all'),
    tabFilterPolygons: document.getElementById('tab-filter-polygons'),
    tabFilterLines: document.getElementById('tab-filter-lines'),
    tabFilterPoints: document.getElementById('tab-filter-points'),
    countAll: document.getElementById('count-all'),
    countPolygons: document.getElementById('count-polygons'),
    countLines: document.getElementById('count-lines'),
    countPoints: document.getElementById('count-points'),
  };

  // Helper: Format Numbers with commas
  function formatNumber(num, decimals = 2) {
    if (num === null || num === undefined || isNaN(num)) return '-';
    return Number(num).toLocaleString(undefined, {
      minimumFractionDigits: 0,
      maximumFractionDigits: decimals,
    });
  }

  // Helper: Format Area
  function formatAreaAlt(m2) {
    if (!m2 || m2 <= 0) return '0.00 hectares';
    const ha = m2 / 10000;
    if (ha >= 100) {
      const km2 = m2 / 1000000;
      return `${formatNumber(km2, 3)} km² (${formatNumber(ha, 2)} ha)`;
    }
    return `${formatNumber(ha, 2)} hectares`;
  }

  // Helper: Format Length
  function formatLengthAlt(m) {
    if (!m || m <= 0) return '0.00 km';
    const km = m / 1000;
    return `${formatNumber(km, 3)} km`;
  }

  // =========================================================================
  // Initialize Map
  // =========================================================================
  function initMap() {
    state.map = L.map('map', {
      zoomControl: false,
      attributionControl: true,
    }).setView([12.9716, 77.5946], 12); // Default Bangalore center

    L.control.zoom({ position: 'bottomright' }).addTo(state.map);

    // Basemaps definition (100% Free & Open, NO API Key Required)
    state.basemapLayers.dark = L.layerGroup([
      L.tileLayer(
        'https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Dark_Gray_Base/MapServer/tile/{z}/{y}/{x}',
        {
          attribution: 'Tiles &copy; Esri &mdash; Esri, DeLorme, NAVTEQ',
          maxZoom: 16,
        }
      ),
      L.tileLayer(
        'https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Dark_Gray_Reference/MapServer/tile/{z}/{y}/{x}',
        {
          attribution: '',
          maxZoom: 16,
        }
      ),
    ]);

    state.basemapLayers.osm = L.tileLayer(
      'https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',
      {
        attribution: '&copy; OpenStreetMap contributors',
        maxZoom: 19,
      }
    );

    state.basemapLayers.satellite = L.tileLayer(
      'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
      {
        attribution: 'Tiles &copy; Esri &mdash; Source: Esri, i-cubed, USDA, USGS',
        maxZoom: 19,
      }
    );

    state.basemapLayers.dark.addTo(state.map);

    // Track mouse coordinates
    state.map.on('mousemove', (e) => {
      dom.mouseCoords.textContent = `Lat: ${e.latlng.lat.toFixed(5)}, Lon: ${e.latlng.lng.toFixed(5)}`;
    });
  }

  // =========================================================================
  // Health Check
  // =========================================================================
  async function checkHealth() {
    try {
      const res = await fetch('/health');
      if (res.ok) {
        const data = await res.json();
        dom.apiStatusText.textContent = `API Online v${data.version}`;
        dom.apiStatusPill.style.borderColor = 'rgba(16, 185, 129, 0.4)';
      } else {
        dom.apiStatusText.textContent = 'API Error';
      }
    } catch (e) {
      dom.apiStatusText.textContent = 'Offline';
      dom.apiStatusPill.style.borderColor = 'rgba(244, 63, 94, 0.4)';
    }
  }

  // =========================================================================
  // File Upload & Demo Loader
  // =========================================================================
  async function uploadFile(file) {
    if (!file) return;

    const formData = new FormData();
    formData.append('file', file);

    dom.uploadProgress.style.display = 'flex';
    dom.progressText.textContent = `Uploading & measuring ${file.name}...`;

    try {
      const response = await fetch('/api/files/', {
        method: 'POST',
        body: formData,
      });

      if (!response.ok) {
        const err = await response.json();
        throw new Error(err.detail || 'Upload failed');
      }

      const fileInfo = await response.json();
      await loadFileData(fileInfo.id);
    } catch (err) {
      alert(`Error processing file: ${err.message}`);
    } finally {
      dom.uploadProgress.style.display = 'none';
      dom.fileInput.value = '';
    }
  }

  async function loadSample(sampleName) {
    dom.uploadProgress.style.display = 'flex';
    dom.progressText.textContent = `Loading demo sample ${sampleName}...`;

    try {
      const response = await fetch(`/api/files/samples/load/${encodeURIComponent(sampleName)}`, {
        method: 'POST',
      });

      if (!response.ok) {
        const err = await response.json();
        throw new Error(err.detail || 'Failed to load sample');
      }

      const fileInfo = await response.json();
      await loadFileData(fileInfo.id);
    } catch (err) {
      alert(`Failed to load sample: ${err.message}`);
    } finally {
      dom.uploadProgress.style.display = 'none';
    }
  }

  // =========================================================================
  // Load & Display Dataset
  // =========================================================================
  async function loadFileData(fileId) {
    try {
      // 1. Fetch file info & measurements
      const [measurementsRes, featuresRes] = await Promise.all([
        fetch(`/api/files/${fileId}/measurements/`),
        fetch(`/api/files/${fileId}/features/?limit=2000&to_wgs84=true`),
      ]);

      if (!measurementsRes.ok || !featuresRes.ok) {
        throw new Error('Failed to retrieve file measurements');
      }

      state.measurementsData = await measurementsRes.json();
      const featBody = await featuresRes.json();
      state.featuresData = featBody.features || [];
      state.activeFile = {
        id: fileId,
        filename: state.measurementsData.filename,
        crs: state.measurementsData.crs,
        summary: state.measurementsData.summary,
      };

      // 2. Render UI
      renderActiveFileOverview();
      renderFeaturesList();
      renderMapFeatures();

      // 3. Reveal panels
      dom.activeFilePanel.style.display = 'block';
      dom.featuresPanel.style.display = 'block';

      // 4. Auto fit map bounds
      fitMapToBounds();
    } catch (err) {
      alert(`Error loading measurements: ${err.message}`);
    }
  }

  function renderActiveFileOverview() {
    const f = state.activeFile;
    const summary = state.measurementsData.summary;

    dom.fileName.textContent = f.filename;
    dom.fileCrs.textContent = f.crs || 'Unknown';
    dom.valTotalArea.textContent = formatNumber(summary.total_area_m2, 2);
    dom.valAreaAlt.textContent = formatAreaAlt(summary.total_area_m2);
    dom.valTotalLength.textContent = formatNumber(summary.total_length_m, 2);
    dom.valLengthAlt.textContent = formatLengthAlt(summary.total_length_m);
    dom.featuresTotalCount.textContent = `${state.measurementsData.total} Features`;

    // Counts by geometry
    const polyCount = state.featuresData.filter((x) => x.geometry_type === 'Polygon' || x.geometry_type === 'MultiPolygon').length;
    const lineCount = state.featuresData.filter((x) => x.geometry_type === 'LineString' || x.geometry_type === 'MultiLineString' || x.geometry_type === 'LinearRing').length;
    const pointCount = state.featuresData.filter((x) => x.geometry_type === 'Point' || x.geometry_type === 'MultiPoint').length;

    dom.countAll.textContent = state.featuresData.length;
    dom.countPolygons.textContent = polyCount;
    dom.countLines.textContent = lineCount;
    dom.countPoints.textContent = pointCount;

    // Breakdown pills
    dom.breakdownPills.innerHTML = `
      <span class="breakdown-pill"><i class="fa-solid fa-draw-polygon text-cyan"></i> ${polyCount} Polygons</span>
      <span class="breakdown-pill"><i class="fa-solid fa-route text-indigo"></i> ${lineCount} Lines</span>
      <span class="breakdown-pill"><i class="fa-solid fa-map-pin text-emerald"></i> ${pointCount} Points</span>
      <span class="breakdown-pill"><i class="fa-solid fa-check text-emerald"></i> ${summary.measured} Measured</span>
    `;
  }

  // =========================================================================
  // Render Features List
  // =========================================================================
  function renderFeaturesList() {
    dom.featuresList.innerHTML = '';
    const query = state.searchQuery.toLowerCase().trim();

    const filtered = state.featuresData.filter((feat) => {
      // Geometry Filter
      if (state.currentFilter === 'Polygon' && !(feat.geometry_type === 'Polygon' || feat.geometry_type === 'MultiPolygon')) return false;
      if (state.currentFilter === 'LineString' && !(feat.geometry_type === 'LineString' || feat.geometry_type === 'MultiLineString' || feat.geometry_type === 'LinearRing')) return false;
      if (state.currentFilter === 'Point' && !(feat.geometry_type === 'Point' || feat.geometry_type === 'MultiPoint')) return false;

      // Search Query
      if (query) {
        const idMatch = (feat.source_id || '').toLowerCase().includes(query);
        const layerMatch = (feat.layer || '').toLowerCase().includes(query);
        const typeMatch = feat.geometry_type.toLowerCase().includes(query);
        const propMatch = JSON.stringify(feat.properties || {}).toLowerCase().includes(query);
        return idMatch || layerMatch || typeMatch || propMatch;
      }
      return true;
    });

    if (filtered.length === 0) {
      dom.featuresList.innerHTML = `<p style="padding: 16px; text-align: center; color: var(--text-subtle); font-size: 0.8rem;">No matching features found.</p>`;
      return;
    }

    filtered.forEach((feat) => {
      const card = document.createElement('div');
      card.className = `feature-card-item ${state.selectedFeatureIndex === feat.index ? 'selected' : ''}`;
      card.id = `feature-card-${feat.index}`;

      let geomClass = 'polygon';
      let geomIcon = 'fa-draw-polygon';
      if (feat.geometry_type.includes('Line')) {
        geomClass = 'linestring';
        geomIcon = 'fa-route';
      } else if (feat.geometry_type.includes('Point')) {
        geomClass = 'point';
        geomIcon = 'fa-map-pin';
      }

      let measureHtml = '';
      if (feat.area_m2 !== null) {
        measureHtml += `<span class="stat-item"><span class="stat-label">Area:</span> ${formatNumber(feat.area_m2)} m²</span>`;
      }
      if (feat.length_m !== null) {
        measureHtml += `<span class="stat-item"><span class="stat-label">Len:</span> ${formatNumber(feat.length_m)} m</span>`;
      }
      if (feat.perimeter_m !== null && feat.area_m2 !== null) {
        measureHtml += `<span class="stat-item"><span class="stat-label">Perim:</span> ${formatNumber(feat.perimeter_m)} m</span>`;
      }
      if (!measureHtml) {
        measureHtml = `<span class="stat-item text-subtle">${feat.status.replace('_', ' ')}</span>`;
      }

      card.innerHTML = `
        <div class="feature-item-header">
          <div class="feature-item-title">
            <span class="geom-tag ${geomClass}"><i class="fa-solid ${geomIcon}"></i></span>
            <span>#${feat.index} ${feat.source_id ? `(${feat.source_id})` : ''}</span>
          </div>
          <span class="badge ${feat.status === 'MEASURED' ? 'badge-success' : 'badge-neutral'}">${feat.status}</span>
        </div>
        <div class="feature-item-stats">
          ${measureHtml}
        </div>
      `;

      card.addEventListener('click', () => {
        selectFeature(feat.index, true);
      });

      dom.featuresList.appendChild(card);
    });
  }

  // =========================================================================
  // Render Features On Map
  // =========================================================================
  function renderMapFeatures() {
    if (state.geojsonLayer) {
      state.map.removeLayer(state.geojsonLayer);
      state.featureLayerMap.clear();
    }

    state.geojsonLayer = L.geoJSON(
      {
        type: 'FeatureCollection',
        features: state.featuresData
          .filter((f) => f.geometry)
          .map((f) => ({
            type: 'Feature',
            geometry: f.geometry,
            properties: {
              ...f.properties,
              __index: f.index,
              __geometry_type: f.geometry_type,
              __area_m2: f.area_m2,
              __perimeter_m: f.perimeter_m,
              __length_m: f.length_m,
              __source_id: f.source_id,
              __layer: f.layer,
              __status: f.status,
              __method: f.method,
            },
          })),
      },
      {
        style: (feature) => {
          const type = feature.properties.__geometry_type;
          if (type === 'Polygon' || type === 'MultiPolygon') {
            return {
              color: '#06b6d4',
              weight: 2,
              opacity: 0.9,
              fillColor: '#06b6d4',
              fillOpacity: 0.25,
            };
          }
          if (type.includes('Line')) {
            return {
              color: '#818cf8',
              weight: 3.5,
              opacity: 0.95,
            };
          }
          return { color: '#10b981' };
        },
        pointToLayer: (feature, latlng) => {
          return L.circleMarker(latlng, {
            radius: 6,
            fillColor: '#10b981',
            color: '#ffffff',
            weight: 2,
            opacity: 1,
            fillOpacity: 0.9,
          });
        },
        onEachFeature: (feature, layer) => {
          const idx = feature.properties.__index;
          state.featureLayerMap.set(idx, layer);

          // Popup content
          const p = feature.properties;
          let popupHtml = `<div class="map-popup-card">
            <div class="popup-title">Feature #${idx} ${p.__source_id ? `(${p.__source_id})` : ''}</div>
            <div class="popup-row"><span class="popup-label">Type:</span> <span class="popup-value">${p.__geometry_type}</span></div>
            <div class="popup-row"><span class="popup-label">Status:</span> <span class="popup-value">${p.__status}</span></div>`;

          if (p.__area_m2 !== null) {
            popupHtml += `<div class="popup-row"><span class="popup-label">Area:</span> <span class="popup-value">${formatNumber(p.__area_m2)} m²</span></div>`;
          }
          if (p.__perimeter_m !== null) {
            popupHtml += `<div class="popup-row"><span class="popup-label">Perimeter:</span> <span class="popup-value">${formatNumber(p.__perimeter_m)} m</span></div>`;
          }
          if (p.__length_m !== null) {
            popupHtml += `<div class="popup-row"><span class="popup-label">Length:</span> <span class="popup-value">${formatNumber(p.__length_m)} m</span></div>`;
          }
          popupHtml += `</div>`;

          layer.bindPopup(popupHtml);

          // Click handler
          layer.on('click', () => {
            selectFeature(idx, false);
          });

          // Hover highlight
          layer.on('mouseover', () => {
            if (state.selectedFeatureIndex !== idx) {
              if (layer.setStyle) {
                layer.setStyle({ weight: 4, opacity: 1, fillOpacity: 0.4 });
              }
            }
          });

          layer.on('mouseout', () => {
            if (state.selectedFeatureIndex !== idx) {
              if (layer.setStyle) {
                state.geojsonLayer.resetStyle(layer);
              }
            }
          });
        },
      }
    ).addTo(state.map);
  }

  function fitMapToBounds() {
    if (state.geojsonLayer && state.geojsonLayer.getLayers().length > 0) {
      state.map.fitBounds(state.geojsonLayer.getBounds(), {
        padding: [40, 40],
        maxZoom: 18,
      });
    }
  }

  // =========================================================================
  // Feature Selection & Inspection Drawer
  // =========================================================================
  function selectFeature(index, panTo = true) {
    state.selectedFeatureIndex = index;
    const feat = state.featuresData.find((f) => f.index === index);
    if (!feat) return;

    // Update List highlight
    document.querySelectorAll('.feature-card-item').forEach((el) => el.classList.remove('selected'));
    const selectedCard = document.getElementById(`feature-card-${index}`);
    if (selectedCard) {
      selectedCard.classList.add('selected');
      selectedCard.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    }

    // Pan map & open popup
    const layer = state.featureLayerMap.get(index);
    if (layer) {
      if (panTo) {
        if (layer.getBounds) {
          state.map.flyToBounds(layer.getBounds(), { padding: [50, 50], maxZoom: 17 });
        } else if (layer.getLatLng) {
          state.map.flyTo(layer.getLatLng(), 16);
        }
      }
      layer.openPopup();
    }

    // Populate Inspection Drawer
    populateInspectionDrawer(feat);
  }

  function populateInspectionDrawer(feat) {
    dom.detailTitle.textContent = feat.source_id ? `Feature: ${feat.source_id}` : `Feature #${feat.index}`;
    dom.detailSubtitle.textContent = `Type: ${feat.geometry_type} | Index #${feat.index}`;
    dom.detailStatusBadge.textContent = feat.status;
    dom.detailStatusBadge.className = `badge ${feat.status === 'MEASURED' ? 'badge-success' : 'badge-neutral'}`;
    dom.detailLayerText.textContent = `Layer: ${feat.layer || 'Default'}`;

    let icon = 'fa-draw-polygon';
    if (feat.geometry_type.includes('Line')) icon = 'fa-route';
    else if (feat.geometry_type.includes('Point')) icon = 'fa-map-pin';
    dom.detailGeomIcon.innerHTML = `<i class="fa-solid ${icon}"></i>`;

    // Measurements Grid
    dom.detailMeasureGrid.innerHTML = '';
    if (feat.area_m2 !== null) {
      dom.detailMeasureGrid.innerHTML += `
        <div class="measure-box">
          <span class="measure-box-label">Area (Planar)</span>
          <span class="measure-box-val">${formatNumber(feat.area_m2)} m²</span>
          <span class="measure-box-sub">${formatAreaAlt(feat.area_m2)}</span>
        </div>
      `;
    }
    if (feat.perimeter_m !== null) {
      dom.detailMeasureGrid.innerHTML += `
        <div class="measure-box">
          <span class="measure-box-label">Perimeter</span>
          <span class="measure-box-val">${formatNumber(feat.perimeter_m)} m</span>
          <span class="measure-box-sub">${formatLengthAlt(feat.perimeter_m)}</span>
        </div>
      `;
    }
    if (feat.length_m !== null) {
      dom.detailMeasureGrid.innerHTML += `
        <div class="measure-box">
          <span class="measure-box-label">Length</span>
          <span class="measure-box-val">${formatNumber(feat.length_m)} m</span>
          <span class="measure-box-sub">${formatLengthAlt(feat.length_m)}</span>
        </div>
      `;
    }
    if (dom.detailMeasureGrid.children.length === 0) {
      dom.detailMeasureGrid.innerHTML = `
        <div class="measure-box" style="grid-column: span 2;">
          <span class="measure-box-label">Status</span>
          <span class="measure-box-val">${feat.status.replace('_', ' ')}</span>
          <span class="measure-box-sub">${feat.warnings && feat.warnings.length ? feat.warnings[0] : 'Points carry no area or length.'}</span>
        </div>
      `;
    }

    // Methodology Card
    const method = feat.method || {};
    dom.detailCrsMethod.innerHTML = `
      <div class="method-row">
        <span class="method-row-label">Feature Source CRS:</span>
        <span class="method-row-val">${feat.crs || 'EPSG:4326'}</span>
      </div>
      <div class="method-row">
        <span class="method-row-label">Projection Strategy:</span>
        <span class="method-row-val">${method.strategy ? method.strategy.toUpperCase() : 'N/A'}</span>
      </div>
      <div class="method-row">
        <span class="method-row-label">Measurement CRS:</span>
        <span class="method-row-val">${method.crs || 'N/A'}</span>
      </div>
    `;

    // Geodesic Card
    const geo = method.geodesic_check;
    if (geo) {
      dom.detailGeodesicBlock.style.display = 'flex';
      const dev = geo.max_deviation_pct || 0;
      dom.detailGeodesicCard.innerHTML = `
        <div class="method-row">
          <span class="method-row-label">Ellipsoidal Verification:</span>
          <span class="accuracy-badge-good">WGS84 Geodesic</span>
        </div>
        ${geo.area_m2 !== undefined ? `
          <div class="method-row">
            <span class="method-row-label">Geodesic Area:</span>
            <span class="method-row-val">${formatNumber(geo.area_m2)} m²</span>
          </div>` : ''}
        ${geo.length_m !== undefined ? `
          <div class="method-row">
            <span class="method-row-label">Geodesic Length:</span>
            <span class="method-row-val">${formatNumber(geo.length_m)} m</span>
          </div>` : ''}
        <div class="method-row">
          <span class="method-row-label">Max Deviation %:</span>
          <span class="method-row-val text-emerald">${dev.toFixed(4)}%</span>
        </div>
      `;
    } else {
      dom.detailGeodesicBlock.style.display = 'none';
    }

    // Properties Table
    const props = feat.properties || {};
    const keys = Object.keys(props);
    if (keys.length > 0) {
      dom.detailPropertiesBody.innerHTML = keys
        .map((k) => `<tr><td>${k}</td><td>${String(props[k])}</td></tr>`)
        .join('');
      document.getElementById('detail-properties-block').style.display = 'flex';
    } else {
      document.getElementById('detail-properties-block').style.display = 'none';
    }

    dom.featureDetailDrawer.style.display = 'flex';
  }

  // =========================================================================
  // Export Handlers
  // =========================================================================
  function exportMeasurementsJson() {
    if (!state.measurementsData) return;
    const blob = new Blob([JSON.stringify(state.measurementsData, null, 2)], {
      type: 'application/json',
    });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `${state.activeFile.filename}_measurements.json`;
    a.click();
    URL.revokeObjectURL(url);
  }

  function exportMeasurementsCsv() {
    if (!state.measurementsData || !state.measurementsData.measurements) return;
    const headers = ['index', 'source_id', 'layer', 'geometry_type', 'crs', 'status', 'area_m2', 'perimeter_m', 'length_m'];
    const rows = state.measurementsData.measurements.map((m) =>
      headers.map((h) => (m[h] !== null && m[h] !== undefined ? `"${String(m[h]).replace(/"/g, '""')}"` : '')).join(',')
    );
    const csvContent = [headers.join(','), ...rows].join('\n');
    const blob = new Blob([csvContent], { type: 'text/csv;charset=utf-8;' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `${state.activeFile.filename}_measurements.csv`;
    a.click();
    URL.revokeObjectURL(url);
  }

  async function deleteActiveFile() {
    if (!state.activeFile) return;
    if (!confirm(`Are you sure you want to delete dataset "${state.activeFile.filename}"?`)) return;

    try {
      const res = await fetch(`/api/files/${state.activeFile.id}/`, {
        method: 'DELETE',
      });
      if (res.ok) {
        state.activeFile = null;
        state.measurementsData = null;
        state.featuresData = [];
        state.selectedFeatureIndex = null;
        if (state.geojsonLayer) state.map.removeLayer(state.geojsonLayer);
        dom.activeFilePanel.style.display = 'none';
        dom.featuresPanel.style.display = 'none';
        dom.featureDetailDrawer.style.display = 'none';
      }
    } catch (e) {
      alert(`Delete error: ${e.message}`);
    }
  }

  // =========================================================================
  // Event Listeners Setup
  // =========================================================================
  function setupEventListeners() {
    // Browse File Button
    dom.browseBtn.addEventListener('click', (e) => {
      e.preventDefault();
      dom.fileInput.click();
    });

    dom.fileInput.addEventListener('change', (e) => {
      if (e.target.files && e.target.files[0]) {
        uploadFile(e.target.files[0]);
      }
    });

    // Dropzone Drag & Drop
    dom.dropzone.addEventListener('dragover', (e) => {
      e.preventDefault();
      dom.dropzone.classList.add('drag-active');
    });

    dom.dropzone.addEventListener('dragleave', () => {
      dom.dropzone.classList.remove('drag-active');
    });

    dom.dropzone.addEventListener('drop', (e) => {
      e.preventDefault();
      dom.dropzone.classList.remove('drag-active');
      if (e.dataTransfer.files && e.dataTransfer.files[0]) {
        uploadFile(e.dataTransfer.files[0]);
      }
    });

    // Sample Buttons
    document.querySelectorAll('.sample-btn').forEach((btn) => {
      btn.addEventListener('click', () => {
        const sampleName = btn.getAttribute('data-sample');
        if (sampleName) loadSample(sampleName);
      });
    });

    // Basemap Switcher
    dom.btnBasemapToggle.addEventListener('click', (e) => {
      e.stopPropagation();
      const show = dom.basemapDropdown.style.display === 'none';
      dom.basemapDropdown.style.display = show ? 'flex' : 'none';
    });

    document.addEventListener('click', () => {
      dom.basemapDropdown.style.display = 'none';
    });

    document.querySelectorAll('.basemap-option').forEach((opt) => {
      opt.addEventListener('click', () => {
        const key = opt.getAttribute('data-basemap');
        if (key && state.basemapLayers[key]) {
          state.map.removeLayer(state.basemapLayers[state.currentBasemap]);
          state.basemapLayers[key].addTo(state.map);
          state.currentBasemap = key;

          document.querySelectorAll('.basemap-option').forEach((o) => o.classList.remove('active'));
          opt.classList.add('active');

          dom.currentBasemapLabel.textContent = opt.textContent.trim();
        }
      });
    });

    // Fit Bounds
    dom.btnFitBounds.addEventListener('click', () => fitMapToBounds());

    // Drawer Close
    dom.detailCloseBtn.addEventListener('click', () => {
      dom.featureDetailDrawer.style.display = 'none';
      state.selectedFeatureIndex = null;
      document.querySelectorAll('.feature-card-item').forEach((el) => el.classList.remove('selected'));
    });

    // Filter Tabs
    document.querySelectorAll('.filter-tab').forEach((tab) => {
      tab.addEventListener('click', () => {
        document.querySelectorAll('.filter-tab').forEach((t) => t.classList.remove('active'));
        tab.classList.add('active');
        state.currentFilter = tab.getAttribute('data-filter');
        renderFeaturesList();
      });
    });

    // Search Input
    dom.featureSearchInput.addEventListener('input', (e) => {
      state.searchQuery = e.target.value;
      renderFeaturesList();
    });

    // Exports & Delete
    dom.exportJsonBtn.addEventListener('click', exportMeasurementsJson);
    dom.exportCsvBtn.addEventListener('click', exportMeasurementsCsv);
    dom.deleteFileBtn.addEventListener('click', deleteActiveFile);
  }

  // =========================================================================
  // App Bootstrapper
  // =========================================================================
  document.addEventListener('DOMContentLoaded', () => {
    initMap();
    checkHealth();
    setupEventListeners();

    // Check if there is already an uploaded file in the database to show
    fetch('/api/files/?limit=1')
      .then((r) => r.json())
      .then((data) => {
        if (data.files && data.files.length > 0) {
          loadFileData(data.files[0].id);
        }
      })
      .catch(() => {});
  });
})();
