/**
 * CrowdSafe Dashboard Controller
 * Dynamic Safety Zone Management System
 */

'use strict';

(function () {
    // ---- State ----
    let currentCapacity = 5;
    let customCapacity = null;
    let wasRed = false;
    let wasCritical = false;
    let lastStatusData = null;
    let pastEvents = [];
    let currentActiveView = 'dashboard';

    // Try restoring custom capacity from localStorage
    try {
        const saved = localStorage.getItem('crowd_custom_capacity');
        if (saved) {
            const parsed = parseInt(saved, 10);
            if (!isNaN(parsed) && parsed > 0) {
                customCapacity = parsed;
            }
        }
    } catch (e) {}

    // ---- Audio Alarm System (Web Audio API) ----
    let audioCtx = null;
    let alarmInterval = null;
    let isAlarmPlaying = false;
    let isAudioMuted = false;
    let masterAlarmGain = null;
    let activeAlarmOscillators = [];

    function initAudio() {
        if (!audioCtx) {
            const AudioContextClass = window.AudioContext || window.webkitAudioContext;
            if (AudioContextClass) {
                audioCtx = new AudioContextClass();
            }
        }
        if (audioCtx && audioCtx.state === 'suspended') {
            audioCtx.resume().catch(() => {});
        }
        return audioCtx;
    }

    // Unlock audio on first user gesture
    ['click', 'keydown', 'touchstart'].forEach(evt => {
        window.addEventListener(evt, () => initAudio(), { passive: true, once: true });
    });

    function playUrgentDualBeep() {
        if (!isAlarmPlaying || isAudioMuted) return;
        const ctx = initAudio();
        if (!ctx) return;
        try {
            const now = ctx.currentTime;
            masterAlarmGain = ctx.createGain();
            masterAlarmGain.gain.setValueAtTime(0.25, now);
            masterAlarmGain.connect(ctx.destination);

            // Tone 1: 880Hz square wave
            const osc1 = ctx.createOscillator();
            osc1.type = 'square';
            osc1.frequency.setValueAtTime(880, now);
            osc1.connect(masterAlarmGain);
            osc1.start(now);
            osc1.stop(now + 0.09);

            // Tone 2: 1000Hz square wave
            const osc2 = ctx.createOscillator();
            osc2.type = 'square';
            osc2.frequency.setValueAtTime(1000, now + 0.11);
            osc2.connect(masterAlarmGain);
            osc2.start(now + 0.11);
            osc2.stop(now + 0.20);

            activeAlarmOscillators = [osc1, osc2];
            osc2.onended = () => {
                activeAlarmOscillators = [];
            };
        } catch (e) {
            console.warn('Alarm audio error:', e);
        }
    }

    function startAudioAlarm() {
        if (isAlarmPlaying) return;
        isAlarmPlaying = true;
        initAudio();
        playUrgentDualBeep();
        if (alarmInterval) clearInterval(alarmInterval);
        alarmInterval = setInterval(playUrgentDualBeep, 360);
        updateAudioButtonUI();
    }

    function stopAudioAlarm() {
        if (!isAlarmPlaying && !alarmInterval) return;
        isAlarmPlaying = false;
        if (alarmInterval) {
            clearInterval(alarmInterval);
            alarmInterval = null;
        }
        if (masterAlarmGain && audioCtx) {
            try {
                masterAlarmGain.gain.setValueAtTime(0, audioCtx.currentTime);
                masterAlarmGain.disconnect();
            } catch (e) {}
        }
        activeAlarmOscillators.forEach(osc => {
            try { osc.stop(); } catch (e) {}
        });
        activeAlarmOscillators = [];
        updateAudioButtonUI();
    }

    window.toggleAudioMute = function () {
        initAudio();
        isAudioMuted = !isAudioMuted;
        if (!isAudioMuted && wasCritical) {
            playUrgentDualBeep();
        }
        updateAudioButtonUI();
    };

    function updateAudioButtonUI() {
        const btn = document.getElementById('audio-toggle-btn');
        const icon = document.getElementById('sound-icon');
        const text = document.getElementById('sound-text');
        const settingsBtn = document.getElementById('settings-audio-btn');

        if (isAudioMuted) {
            if (btn) btn.className = 'btn-action sound-action muted';
            if (icon) icon.textContent = '🔇';
            if (text) text.textContent = 'Sound: Muted';
            if (settingsBtn) settingsBtn.textContent = 'Unmute Alarm';
        } else if (isAlarmPlaying) {
            if (btn) btn.className = 'btn-action sound-action sounding';
            if (icon) icon.textContent = '🔊';
            if (text) text.textContent = 'Alarm: CRITICAL';
            if (settingsBtn) settingsBtn.textContent = 'Mute Alarm';
        } else {
            if (btn) btn.className = 'btn-action sound-action active';
            if (icon) icon.textContent = '🔊';
            if (text) text.textContent = 'Audio Alarm: ON';
            if (settingsBtn) settingsBtn.textContent = 'Mute Alarm';
        }
    }

    // ---- Live Clock and Date ----
    function updateLiveClock() {
        const now = new Date();
        const timeEl = document.getElementById('live-clock');
        const dateEl = document.getElementById('live-date');

        if (timeEl) {
            timeEl.textContent = now.toLocaleTimeString('en-US', {
                hour: '2-digit',
                minute: '2-digit',
                second: '2-digit',
                hour12: true
            }).toLowerCase();
        }
        if (dateEl) {
            dateEl.textContent = now.toLocaleDateString('en-GB', {
                day: '2-digit',
                month: 'short',
                year: 'numeric'
            });
        }
    }
    setInterval(updateLiveClock, 1000);
    updateLiveClock();

    // ---- View Switching (Sidebar Navigation) ----
    window.switchView = function (viewName) {
        currentActiveView = viewName;
        
        // Update nav items
        ['dashboard', 'monitoring', 'alerts', 'analytics', 'settings'].forEach(v => {
            const btn = document.getElementById(`nav-${v}`);
            const panel = document.getElementById(`view-${v}`);
            if (btn) btn.classList.toggle('active', v === viewName);
            if (panel) panel.classList.toggle('active', v === viewName);
        });

        // Trigger view-specific loads
        if (viewName === 'analytics') {
            fetchPastData();
        }
        if (viewName === 'dashboard' && lastStatusData) {
            drawCrowdGraph(lastStatusData.history, getEffectiveCapacity(), lastStatusData.zone_color);
        }
    };

    // ---- Threshold & Zone Calculation ----
    function getEffectiveCapacity() {
        if (customCapacity !== null && customCapacity > 0) {
            return customCapacity;
        }
        return currentCapacity > 0 ? currentCapacity : 5;
    }

    function calculateThresholdRanges(capacity) {
        const medMin = Math.ceil(capacity * 0.6);
        const critMin = Math.ceil(capacity * 0.9);
        const safeMax = Math.max(0, medMin - 1);
        const medMax = Math.max(medMin, critMin - 1);

        return {
            medMin,
            critMin,
            safeMax,
            medMax
        };
    }

    function updateThresholdPills(capacity) {
        const ranges = calculateThresholdRanges(capacity);

        const safeEl = document.getElementById('thresh-safe-text');
        const medEl = document.getElementById('thresh-med-text');
        const critEl = document.getElementById('thresh-crit-text');

        if (safeEl) safeEl.textContent = `< ${ranges.medMin} (0-${ranges.safeMax})`;
        if (medEl) medEl.textContent = `${ranges.medMin} - ${ranges.medMax}`;
        if (critEl) critEl.textContent = `≥ ${ranges.critMin}`;
    }

    function evaluateZone(peopleInside, capacity) {
        const ranges = calculateThresholdRanges(capacity);

        if (peopleInside >= ranges.critMin) {
            return {
                status: 'critical',
                bannerClass: 'critical',
                icon: '🚨',
                text: 'CRITICAL',
                isCritical: true
            };
        } else if (peopleInside >= ranges.medMin) {
            return {
                status: 'medium',
                bannerClass: 'medium',
                icon: '⚠️',
                text: 'MEDIUM',
                isCritical: false
            };
        } else {
            return {
                status: 'safe',
                bannerClass: 'safe',
                icon: '✓',
                text: 'SAFE',
                isCritical: false
            };
        }
    }

    // ---- Capacity Management Modal & Controls ----
    window.openCapacityModal = function () {
        initAudio();
        const modal = document.getElementById('capacity-modal');
        const input = document.getElementById('modal-capacity-input');
        if (modal && input) {
            input.value = getEffectiveCapacity();
            updateModalThresholdPreview(input.value);
            modal.style.display = 'flex';
            input.focus();
        }
    };

    window.closeCapacityModal = function () {
        const modal = document.getElementById('capacity-modal');
        if (modal) modal.style.display = 'none';
    };

    function updateModalThresholdPreview(val) {
        const preview = document.getElementById('modal-thresh-preview');
        const parsed = parseInt(val, 10);
        if (preview && !isNaN(parsed) && parsed > 0) {
            const ranges = calculateThresholdRanges(parsed);
            preview.innerHTML = `<span>Safe: &lt; ${ranges.medMin}</span> | <span>Medium: ${ranges.medMin} - ${ranges.medMax}</span> | <span>Critical: &ge; ${ranges.critMin}</span>`;
        }
    }

    const modalInput = document.getElementById('modal-capacity-input');
    if (modalInput) {
        modalInput.addEventListener('input', () => updateModalThresholdPreview(modalInput.value));
    }

    window.submitModalCapacity = function () {
        const input = document.getElementById('modal-capacity-input');
        if (!input) return;
        const parsed = parseInt(input.value.trim(), 10);
        if (isNaN(parsed) || parsed <= 0) {
            alert('Please enter a valid positive number for venue capacity.');
            return;
        }

        applyNewCapacity(parsed);
        closeCapacityModal();
    };

    window.applySettingsCapacity = function () {
        const input = document.getElementById('settings-cap-input');
        if (!input) return;
        const parsed = parseInt(input.value.trim(), 10);
        if (isNaN(parsed) || parsed <= 0) {
            alert('Please enter a valid positive number for venue capacity.');
            return;
        }

        applyNewCapacity(parsed);
        alert(`Venue maximum capacity updated to ${parsed}`);
    };

    function applyNewCapacity(newCap) {
        customCapacity = newCap;
        currentCapacity = newCap;
        try {
            localStorage.setItem('crowd_custom_capacity', newCap);
        } catch (e) {}

        // Notify backend server
        fetch(`/set_capacity?capacity=${encodeURIComponent(newCap)}`)
            .catch(err => console.warn('Could not sync capacity with server:', err));

        // Re-render immediately
        if (lastStatusData) {
            renderDashboard(lastStatusData);
        }
    }

    window.resetCounters = function () {
        if (!confirm('Are you sure you want to reset entry and exit counters to 0?')) return;
        fetch('/reset', { method: 'POST' })
            .then(res => res.json())
            .then(() => refreshStatus())
            .catch(err => console.error('Reset error:', err));
    };

    // ---- Dashboard Rendering ----
    function renderDashboard(data) {
        lastStatusData = data;
        if (data.max_capacity && data.max_capacity > 0) {
            currentCapacity = data.max_capacity;
        }
        const effectiveCap = getEffectiveCapacity();
        const peopleInside = data.people_inside != null ? data.people_inside : Math.max(0, (data.entry_count || 0) - (data.exit_count || 0));

        // Update 5 Metrics Cards
        const entryNumEl = document.getElementById('entry-num');
        const exitNumEl = document.getElementById('exit-num');
        const insideNumEl = document.getElementById('inside-num');
        const capNumEl = document.getElementById('capacity-num');
        const frameNumEl = document.getElementById('frame-num');

        if (entryNumEl) entryNumEl.textContent = data.entry_count != null ? data.entry_count : 0;
        if (exitNumEl) exitNumEl.textContent = data.exit_count != null ? data.exit_count : 0;
        if (insideNumEl) insideNumEl.textContent = peopleInside;
        if (capNumEl) capNumEl.textContent = effectiveCap;
        if (frameNumEl) frameNumEl.textContent = data.people_in_frame != null ? data.people_in_frame : 0;

        // Settings input sync
        const settingsCapInput = document.getElementById('settings-cap-input');
        if (settingsCapInput && document.activeElement !== settingsCapInput) {
            settingsCapInput.value = effectiveCap;
        }

        // Update Threshold Pills
        updateThresholdPills(effectiveCap);

        // Evaluate Zone
        const zone = evaluateZone(peopleInside, effectiveCap);

        // Update Status Banner
        const banner = document.getElementById('status-banner');
        const bannerIcon = document.getElementById('banner-icon');
        const bannerText = document.getElementById('banner-text');

        if (banner) {
            banner.className = `status-banner ${zone.bannerClass}`;
        }
        if (bannerIcon) bannerIcon.textContent = zone.icon;
        if (bannerText) bannerText.textContent = zone.text;

        // Overcrowding Directive Banner
        const critBox = document.getElementById('critical-alert-box');
        const overcrowdExitNum = document.getElementById('overcrowd-exit-num');

        if (zone.isCritical) {
            const ranges = calculateThresholdRanges(effectiveCap);
            const neededExits = Math.max(1, peopleInside - ranges.medMax);
            if (overcrowdExitNum) overcrowdExitNum.textContent = neededExits;
            if (critBox) critBox.style.display = 'block';

            startAudioAlarm();

            // Flash overlay once on critical onset
            if (!wasCritical) {
                const overlay = document.getElementById('critical-overlay');
                if (overlay) {
                    overlay.classList.remove('flash');
                    void overlay.offsetWidth;
                    overlay.classList.add('flash');
                }
            }
        } else {
            if (critBox) critBox.style.display = 'none';
            stopAudioAlarm();
        }

        wasCritical = zone.isCritical;
        // Security Threat Detection Alert Banner
        const threatBanner = document.getElementById('threat-alert-banner');
        const threatBannerText = document.getElementById('threat-banner-text');

        if (data.threat_detected && data.threat_alert) {
            const weapons = data.threat_alert.threat_type;
            const weaponStr = Array.isArray(weapons) ? weapons.join(', ').toUpperCase() : String(weapons).toUpperCase();
            if (threatBannerText) {
                threatBannerText.textContent = `🚨 SECURITY ALERT: ${weaponStr} DETECTED AT GATE`;
            }
            if (threatBanner) {
                threatBanner.style.display = 'block';
            }
            startAudioAlarm();
        } else {
            if (threatBanner) {
                threatBanner.style.display = 'none';
            }
        }

        // Draw Live Occupancy Trend
        drawCrowdGraph(data.history, effectiveCap, zone.status);

        // Update Alerts
        if (data.alerts) {
            renderAlertsList(data.alerts);
        }

        // Update Monitoring Feed Images
        updateMonitoringFeeds(data);
    }

    // ---- Clear Threat Alert Handler ----
    window.clearThreatAlert = function () {
        fetch('/clear_threat', { method: 'POST' })
            .then(res => res.json())
            .then(() => {
                const threatBanner = document.getElementById('threat-alert-banner');
                if (threatBanner) threatBanner.style.display = 'none';
                refreshStatus();
            })
            .catch(err => console.error('Clear threat error:', err));
    };

    // ---- Monitoring Feeds Update ----
    function updateMonitoringFeeds(data) {
        if (data.latest_image) {
            const camImg = document.getElementById('live-camera-img');
            if (camImg) {
                camImg.src = `/static/captures/${encodeURIComponent(data.latest_image)}`;
            }
        }
        if (data.latest_heatmap) {
            const heatImg = document.getElementById('live-heatmap-img');
            if (heatImg) {
                heatImg.src = `/static/captures/${encodeURIComponent(data.latest_heatmap)}`;
            }
        }

        const camDetected = document.getElementById('cam-detected-count');
        if (camDetected) {
            const count = data.people_in_frame || 0;
            camDetected.textContent = `${count} ${count === 1 ? 'human' : 'humans'}`;
        }
    }

    // ---- Render Alerts List ----
    function renderAlertsList(alerts) {
        const listEl = document.getElementById('alert-list');
        const badgeCount = document.getElementById('nav-alert-count');
        const totalCount = document.getElementById('alerts-total-count');

        if (totalCount) {
            totalCount.textContent = `${alerts.length} events logged`;
        }
        if (badgeCount) {
            if (alerts.length > 0) {
                badgeCount.textContent = alerts.length;
                badgeCount.style.display = 'inline-block';
            } else {
                badgeCount.style.display = 'none';
            }
        }

        if (!listEl) return;
        if (!alerts || alerts.length === 0) {
            listEl.innerHTML = '<div class="empty-state">No alerts recorded yet. System running smoothly.</div>';
            return;
        }

        listEl.innerHTML = alerts.slice(0, 50).map(a => {
            const typeClass = (a.type || 'info').toLowerCase();
            return `<div class="alert-row ${typeClass}">
                <span class="time">${a.time || '--:--'}</span>
                <span class="message">${escapeHtml(a.message || '')}</span>
            </div>`;
        }).join('');
    }

    window.refreshAlerts = function () {
        refreshStatus();
    };

    function escapeHtml(str) {
        return str.replace(/[&<>'"]/g, tag => ({
            '&': '&amp;',
            '<': '&lt;',
            '>': '&gt;',
            "'": '&#39;',
            '"': '&quot;'
        }[tag] || tag));
    }

    // ---- Draw Crowd Graph (HTML5 Canvas) ----
    function drawCrowdGraph(history, maxCapacity, zoneStatus) {
        const canvas = document.getElementById('crowd-canvas');
        if (!canvas) return;
        const ctx = canvas.getContext('2d');
        const W = canvas.width;
        const H = canvas.height;

        const padL = 45;
        const padR = 20;
        const padT = 25;
        const padB = 35;
        const plotW = W - padL - padR;
        const plotH = H - padT - padB;

        ctx.clearRect(0, 0, W, H);

        if (!history || history.length === 0) {
            ctx.fillStyle = '#64748b';
            ctx.font = '14px sans-serif';
            ctx.textAlign = 'center';
            ctx.fillText('Awaiting real-time occupancy sensor stream...', W / 2, H / 2);
            return;
        }

        const maxVal = Math.max(maxCapacity, ...history.map(p => p.value || 0)) + 1;
        const statusColors = {
            safe: '#10b981',
            medium: '#f59e0b',
            critical: '#ef4444'
        };
        const lineColor = statusColors[zoneStatus] || '#0ea5e9';

        // Gridlines & Y-Axis Labels
        ctx.strokeStyle = 'rgba(59, 130, 246, 0.12)';
        ctx.fillStyle = '#94a3b8';
        ctx.font = '11px sans-serif';
        ctx.textAlign = 'right';

        const ySteps = 4;
        for (let i = 0; i <= ySteps; i++) {
            const val = Math.round((maxVal / ySteps) * i);
            const y = padT + plotH - (val / maxVal) * plotH;
            ctx.beginPath();
            ctx.moveTo(padL, y);
            ctx.lineTo(W - padR, y);
            ctx.stroke();
            ctx.fillText(val, padL - 8, y + 4);
        }

        // Capacity Limit Line (Dashed Red)
        const capY = padT + plotH - (maxCapacity / maxVal) * plotH;
        ctx.save();
        ctx.setLineDash([6, 5]);
        ctx.strokeStyle = '#ef4444';
        ctx.lineWidth = 1.8;
        ctx.beginPath();
        ctx.moveTo(padL, capY);
        ctx.lineTo(W - padR, capY);
        ctx.stroke();
        ctx.restore();

        // Occupancy Trend Curve
        const stepX = plotW / Math.max(history.length - 1, 1);
        ctx.beginPath();
        history.forEach((point, i) => {
            const x = padL + i * stepX;
            const y = padT + plotH - ((point.value || 0) / maxVal) * plotH;
            if (i === 0) ctx.moveTo(x, y);
            else ctx.lineTo(x, y);
        });
        ctx.strokeStyle = lineColor;
        ctx.lineWidth = 3;
        ctx.stroke();

        // Gradient Glow Under Curve
        ctx.lineTo(padL + (history.length - 1) * stepX, padT + plotH);
        ctx.lineTo(padL, padT + plotH);
        ctx.closePath();
        const grad = ctx.createLinearGradient(0, padT, 0, padT + plotH);
        grad.addColorStop(0, `${lineColor}40`);
        grad.addColorStop(1, `${lineColor}00`);
        ctx.fillStyle = grad;
        ctx.fill();

        // Highlight latest point
        const last = history[history.length - 1];
        const lastX = padL + (history.length - 1) * stepX;
        const lastY = padT + plotH - ((last.value || 0) / maxVal) * plotH;
        ctx.beginPath();
        ctx.arc(lastX, lastY, 5, 0, Math.PI * 2);
        ctx.fillStyle = lineColor;
        ctx.fill();
        ctx.lineWidth = 2;
        ctx.strokeStyle = '#ffffff';
        ctx.stroke();

        // X-Axis Timestamps
        ctx.fillStyle = '#64748b';
        ctx.textAlign = 'left';
        ctx.fillText(history[0].time || '', padL, H - 10);
        ctx.textAlign = 'center';
        ctx.fillText(history[Math.floor(history.length / 2)].time || '', padL + plotW / 2, H - 10);
        ctx.textAlign = 'right';
        ctx.fillText(last.time || '', W - padR, H - 10);
    }

    // ---- Past Data & Hourly Chart (Analytics View) ----
    function drawHourlyChart(hourly) {
        const canvas = document.getElementById('hourly-canvas');
        if (!canvas) return;
        const ctx = canvas.getContext('2d');
        const W = canvas.width;
        const H = canvas.height;

        const padL = 45;
        const padR = 20;
        const padT = 20;
        const padB = 40;
        const plotW = W - padL - padR;
        const plotH = H - padT - padB;

        ctx.clearRect(0, 0, W, H);

        if (!hourly || hourly.length === 0) {
            ctx.fillStyle = '#64748b';
            ctx.font = '14px sans-serif';
            ctx.textAlign = 'center';
            ctx.fillText('No hourly gate traffic recorded yet', W / 2, H / 2);
            return;
        }

        let yMax = 1;
        hourly.forEach(item => {
            yMax = Math.max(yMax, Number(item.entries) || 0, Number(item.exits) || 0);
        });
        yMax = Math.ceil(yMax * 1.2);

        // Grid lines
        ctx.strokeStyle = 'rgba(59, 130, 246, 0.12)';
        ctx.fillStyle = '#94a3b8';
        ctx.font = '11px sans-serif';
        ctx.textAlign = 'right';
        for (let i = 0; i <= 4; i++) {
            const val = Math.round((yMax * i) / 4);
            const y = padT + plotH - (val / yMax) * plotH;
            ctx.beginPath();
            ctx.moveTo(padL, y);
            ctx.lineTo(W - padR, y);
            ctx.stroke();
            ctx.fillText(val, padL - 8, y + 4);
        }

        const groupW = plotW / hourly.length;
        const barW = Math.min(16, groupW * 0.32);

        hourly.forEach((item, index) => {
            const centerX = padL + groupW * (index + 0.5);
            const entryH = ((Number(item.entries) || 0) / yMax) * plotH;
            const exitH = ((Number(item.exits) || 0) / yMax) * plotH;

            // Entry Bar (Cyan/Blue)
            ctx.fillStyle = '#38bdf8';
            ctx.fillRect(centerX - barW - 1, padT + plotH - entryH, barW, entryH);

            // Exit Bar (Purple)
            ctx.fillStyle = '#a855f7';
            ctx.fillRect(centerX + 1, padT + plotH - exitH, barW, exitH);

            // X-Axis Hour Label
            if (index % Math.max(1, Math.ceil(hourly.length / 8)) === 0 || index === hourly.length - 1) {
                const hourText = String(item.hour || '').slice(11, 16);
                ctx.fillStyle = '#64748b';
                ctx.textAlign = 'center';
                ctx.fillText(hourText, centerX, H - 12);
            }
        });
    }

    function renderPastEventsTable() {
        const tbody = document.getElementById('past-events');
        const filter = document.getElementById('past-filter').value;
        if (!tbody) return;

        const filtered = pastEvents.filter(ev => {
            if (filter === 'ALL') return true;
            if (filter === 'OBJECT') {
                return ev.direction === 'OBJECT' || (ev.trigger_type && ev.trigger_type.toLowerCase().includes('object'));
            }
            if (filter === 'TRIGGER') {
                return ev.direction === 'TRIGGER' || ev.direction === 'VERIFY';
            }
            return ev.direction === filter;
        });

        tbody.replaceChildren();

        if (filtered.length === 0) {
            const row = document.createElement('tr');
            const cell = document.createElement('td');
            cell.colSpan = 6;
            cell.className = 'text-center';
            cell.textContent = 'No matching crowd events found in audit log.';
            row.appendChild(cell);
            tbody.appendChild(row);
            return;
        }

        filtered.forEach(ev => {
            const row = document.createElement('tr');

            // Time
            const timeCell = document.createElement('td');
            timeCell.textContent = ev.timestamp || '-';
            row.appendChild(timeCell);

            // Event Badge
            const eventCell = document.createElement('td');
            const badge = document.createElement('span');
            const dir = String(ev.direction || '').toUpperCase();
            badge.className = 'event-badge';
            badge.style.backgroundColor = dir === 'ENTRY' ? '#38bdf8' :
                                         dir === 'EXIT' ? '#a855f7' :
                                         dir === 'OBJECT' ? '#f59e0b' :
                                         dir === 'TRIGGER' ? '#10b981' : '#64748b';
            badge.textContent = dir || '-';
            eventCell.appendChild(badge);
            row.appendChild(eventCell);

            // People count
            const peopleCell = document.createElement('td');
            peopleCell.textContent = ev.people_count != null ? ev.people_count : '-';
            row.appendChild(peopleCell);

            // Occupancy
            const occCell = document.createElement('td');
            occCell.textContent = ev.current_occupancy != null ? ev.current_occupancy : '-';
            row.appendChild(occCell);

            // Image Link
            const imgCell = document.createElement('td');
            if (ev.image_filename) {
                const link = document.createElement('a');
                link.href = `/static/captures/${encodeURIComponent(ev.image_filename)}`;
                link.target = '_blank';
                link.rel = 'noopener noreferrer';
                link.style.color = '#38bdf8';
                link.textContent = `📷 ${ev.image_filename.slice(0, 24)}...`;
                imgCell.appendChild(link);
            } else {
                imgCell.textContent = '-';
            }
            row.appendChild(imgCell);

            // Trigger Type
            const trigCell = document.createElement('td');
            const trigBadge = document.createElement('span');
            const rawType = String(ev.trigger_type || (ev.direction === 'OBJECT' ? 'Object' : 'Human'));
            const isHuman = rawType.toLowerCase().includes('human') && ev.direction !== 'OBJECT';
            trigBadge.className = `trigger-badge ${isHuman ? 'human' : 'object'}`;
            trigBadge.textContent = isHuman ? '👤 Human' : '📦 Object';
            trigCell.appendChild(trigBadge);
            row.appendChild(trigCell);

            tbody.appendChild(row);
        });
    }

    function fetchPastData() {
        const limitEl = document.getElementById('past-limit');
        const limit = limitEl ? limitEl.value : 50;
        const errBox = document.getElementById('past-error');
        if (errBox) errBox.textContent = '';

        Promise.all([
            fetch(`/history?limit=${encodeURIComponent(limit)}`).then(r => {
                if (!r.ok) throw new Error(`History error ${r.status}`);
                return r.json();
            }),
            fetch('/hourly?hours=24').then(r => {
                if (!r.ok) throw new Error(`Hourly error ${r.status}`);
                return r.json();
            })
        ]).then(([histData, hourlyData]) => {
            const stats = histData.stats || {};
            const totalEntries = document.getElementById('past-total-entries');
            const totalExits = document.getElementById('past-total-exits');
            const peakOccupancy = document.getElementById('past-peak-occupancy');
            const totalEvents = document.getElementById('past-total-events');

            if (totalEntries) totalEntries.textContent = stats.total_entries || 0;
            if (totalExits) totalExits.textContent = stats.total_exits || 0;
            if (peakOccupancy) peakOccupancy.textContent = stats.peak_occupancy || 0;
            if (totalEvents) totalEvents.textContent = stats.total_events || 0;

            pastEvents = Array.isArray(histData.events) ? histData.events : [];
            renderPastEventsTable();
            drawHourlyChart(hourlyData);
        }).catch(err => {
            if (errBox) errBox.textContent = `Could not load past data: ${err.message}`;
        });
    }

    const pastRefreshBtn = document.getElementById('past-refresh');
    const pastLimitSel = document.getElementById('past-limit');
    const pastFilterSel = document.getElementById('past-filter');

    if (pastRefreshBtn) pastRefreshBtn.addEventListener('click', fetchPastData);
    if (pastLimitSel) pastLimitSel.addEventListener('change', fetchPastData);
    if (pastFilterSel) pastFilterSel.addEventListener('change', renderPastEventsTable);

    // ---- Status Polling Loop ----
    function refreshStatus() {
        fetch('/status')
            .then(res => res.json())
            .then(data => {
                renderDashboard(data);
            })
            .catch(err => {
                console.warn('Status poll warning:', err);
            });
    }

    // Initial fetch and intervals
    refreshStatus();
    setInterval(refreshStatus, 1500);

    // Periodic past data reload when in Analytics view
    setInterval(() => {
        if (currentActiveView === 'analytics') {
            fetchPastData();
        }
    }, 6000);

})();
