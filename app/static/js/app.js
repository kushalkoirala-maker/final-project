(function () {
  function readJsonSafe(response) {
    return response.json().catch(function () {
      return {};
    });
  }

  function buildVariablesFromFields(container) {
    var jsonField = container.querySelector("[data-var-json='1']");
    if (jsonField) {
      var raw = (jsonField.value || "").trim();
      if (!raw) {
        return {};
      }
      try {
        var parsed = JSON.parse(raw);
        if (parsed === null || Array.isArray(parsed) || typeof parsed !== "object") {
          throw new Error("Variables JSON must be an object.");
        }
        return parsed;
      } catch (err) {
        throw new Error("Invalid variables JSON: " + err.message);
      }
    }

    var values = {};
    var fields = container.querySelectorAll("[data-var-name]");
    fields.forEach(function (input) {
      var key = input.getAttribute("data-var-name");
      var value = (input.value || "").trim();
      if (value === "") {
        return;
      }
      if (input.getAttribute("data-var-type") === "number") {
        var parsed = Number(value);
        values[key] = Number.isNaN(parsed) ? value : parsed;
      } else {
        values[key] = value;
      }
    });
    return values;
  }

  function formatApiError(data, fallback) {
    if (data && data.validation && Array.isArray(data.validation.reasons) && data.validation.reasons.length) {
      return data.validation.reasons.join("\n");
    }
    if (data && data.error) {
      return data.error;
    }
    return fallback || "Unknown error";
  }

  function renderDynamicFields(container, schema) {
    container.innerHTML = "";
    var fields = (schema && schema.fields) || [];
    if (!fields.length) {
      var wrap = document.createElement("div");
      wrap.style.flex = "1 1 100%";

      var label = document.createElement("label");
      label.textContent = "Variables (JSON object)";

      var area = document.createElement("textarea");
      area.rows = 8;
      area.placeholder = '{\"key\": \"value\"}';
      area.setAttribute("data-var-json", "1");

      wrap.appendChild(label);
      wrap.appendChild(area);
      container.appendChild(wrap);
      return;
    }

    fields.forEach(function (field) {
      var wrap = document.createElement("div");
      var label = document.createElement("label");
      label.textContent = field.label + (field.required ? " *" : "");

      var input = document.createElement("input");
      input.type = field.type === "number" ? "number" : "text";
      input.required = !!field.required;
      if (field.placeholder) {
        input.placeholder = field.placeholder;
      }
      input.setAttribute("data-var-name", field.name);
      input.setAttribute("data-var-type", field.type || "text");

      wrap.appendChild(label);
      wrap.appendChild(input);
      container.appendChild(wrap);
    });
  }

  function initCommandRunner() {
    var form = document.getElementById("command-run-form");
    if (!form) {
      return;
    }

    var commandInput = document.getElementById("command-input");
    var submitButton = document.getElementById("command-submit");
    var statusEl = document.getElementById("command-status");
    var outputEl = document.getElementById("command-output");
    var endpoint = form.getAttribute("data-endpoint");

    function setRunningState(isRunning) {
      submitButton.disabled = isRunning;
      commandInput.disabled = isRunning;
      submitButton.textContent = isRunning ? "Running..." : "Run";
    }

    form.addEventListener("submit", function (event) {
      event.preventDefault();

      var command = (commandInput.value || "").trim();
      if (!command) {
        statusEl.textContent = "Command is required.";
        outputEl.textContent = "";
        return;
      }

      setRunningState(true);
      statusEl.textContent = "Running command...";
      outputEl.textContent = "";

      fetch(endpoint, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "same-origin",
        body: JSON.stringify({ command: command })
      })
        .then(function (response) {
          return readJsonSafe(response).then(function (data) {
            return { ok: response.ok, status: response.status, data: data };
          });
        })
        .then(function (result) {
          var data = result.data || {};
          var output = data.output || "";
          var error = data.error || "";

          if (result.ok && data.success) {
            statusEl.textContent = "Command completed successfully.";
            outputEl.textContent = output || "(no output)";
            return;
          }

          statusEl.textContent = "Command failed (HTTP " + result.status + ").";
          outputEl.textContent = (error ? "Error: " + error + "\n\n" : "") + (output || "");
        })
        .catch(function (err) {
          statusEl.textContent = "Request failed.";
          outputEl.textContent = "Error: " + err;
        })
        .finally(function () {
          setRunningState(false);
        });
    });
  }

  function initJobDetailPolling() {
    var holder = document.getElementById("job-detail");
    if (!holder) {
      return;
    }

    var jobId = holder.getAttribute("data-job-id");
    var statusBadge = document.getElementById("job-status-badge");
    var startedEl = document.getElementById("job-started");
    var finishedEl = document.getElementById("job-finished");
    var resultEl = document.getElementById("job-result");
    var pollNote = document.getElementById("job-poll-note");
    var deviceResultsBody = document.getElementById("job-device-results-body");

    function renderDeviceResults(rows) {
      if (!deviceResultsBody) {
        return;
      }
      if (!rows || !rows.length) {
        deviceResultsBody.innerHTML = '<tr><td colspan="4">No per-device rows.</td></tr>';
        return;
      }
      var html = rows.map(function (row) {
        var status = row.status || "unknown";
        var label = "Device #" + row.device_id;
        return (
          "<tr>" +
            "<td>" + label + "</td>" +
            '<td><span class="badge status-' + status + '">' + status + "</span></td>" +
            "<td>" + (row.started_at || "-") + "</td>" +
            "<td>" + (row.finished_at || "-") + "</td>" +
          "</tr>"
        );
      }).join("");
      deviceResultsBody.innerHTML = html;
    }

    function update() {
      fetch("/api/jobs/" + jobId, { credentials: "same-origin" })
        .then(function (response) {
          return readJsonSafe(response).then(function (data) {
            return { ok: response.ok, data: data };
          });
        })
        .then(function (res) {
          if (!res.ok || !res.data || !res.data.job) {
            pollNote.textContent = "Failed to poll job status.";
            return;
          }

          var job = res.data.job;
          statusBadge.textContent = job.status;
          statusBadge.className = "badge status-" + job.status;
          startedEl.textContent = job.started_at || "-";
          finishedEl.textContent = job.finished_at || "-";
          resultEl.textContent = job.result_text || "(no result)";
          renderDeviceResults(job.device_results || []);

          if (job.status === "success" || job.status === "failed") {
            pollNote.textContent = "Job finished.";
            clearInterval(intervalId);
          }
        })
        .catch(function () {
          pollNote.textContent = "Polling error.";
        });
    }

    var intervalId = setInterval(update, 2000);
    update();
  }

  function initTemplatesTool() {
    var tool = document.getElementById("templates-tool");
    if (!tool) {
      return;
    }

    var deviceEl = document.getElementById("tpl-device");
    var templateEl = document.getElementById("tpl-name");
    var fieldsEl = document.getElementById("tpl-fields");
    var descEl = document.getElementById("tpl-description");
    var statusEl = document.getElementById("tpl-status");
    var outputEl = document.getElementById("tpl-output");
    var jobLinkEl = document.getElementById("tpl-job-link");
    var allowRisky = document.getElementById("tpl-allow-risky");
    var previewBtn = document.getElementById("tpl-preview-btn");
    var diffBtn = document.getElementById("tpl-diff-btn");
    var applyBtn = document.getElementById("tpl-apply-btn");

    var schemas = JSON.parse(tool.getAttribute("data-template-schemas") || "{}");

    function selectedTemplate() {
      return (templateEl.value || "").trim();
    }

    function selectedDevice() {
      return (deviceEl.value || "").trim();
    }

    function syncTemplateFields() {
      var schema = schemas[selectedTemplate()] || { fields: [], description: "" };
      descEl.textContent = schema.description || "No schema found for this template. Use JSON variables.";
      renderDynamicFields(fieldsEl, schema);
      if (jobLinkEl) {
        jobLinkEl.textContent = "";
      }
    }

    templateEl.addEventListener("change", syncTemplateFields);
    syncTemplateFields();

    function buildPayload() {
      return {
        template: selectedTemplate(),
        variables: buildVariablesFromFields(fieldsEl),
        allow_risky_commands: !!(allowRisky && allowRisky.checked)
      };
    }

    previewBtn.addEventListener("click", function () {
      var payload;
      try {
        payload = buildPayload();
      } catch (err) {
        statusEl.textContent = "Preview failed.";
        outputEl.textContent = String(err.message || err);
        return;
      }
      if (!payload.template) {
        statusEl.textContent = "Choose a template first.";
        return;
      }

      statusEl.textContent = "Previewing commands...";
      outputEl.textContent = "";
      fetch("/api/templates/preview", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "same-origin",
        body: JSON.stringify({
          template: payload.template,
          variables: payload.variables,
          allow_risky_commands: payload.allow_risky_commands
        })
      })
        .then(function (response) {
          return readJsonSafe(response).then(function (data) {
            return { ok: response.ok, status: response.status, data: data };
          });
        })
        .then(function (res) {
          if (!res.ok) {
            statusEl.textContent = "Preview failed (HTTP " + res.status + ").";
            outputEl.textContent = formatApiError(res.data, "Unknown error");
            return;
          }
          statusEl.textContent = "Preview generated.";
          outputEl.textContent = (res.data.commands || []).join("\n") || "(no commands)";
        })
        .catch(function (err) {
          statusEl.textContent = "Preview failed.";
          outputEl.textContent = String(err);
        });
    });

    diffBtn.addEventListener("click", function () {
      var payload;
      try {
        payload = buildPayload();
      } catch (err) {
        statusEl.textContent = "Diff failed.";
        outputEl.textContent = String(err.message || err);
        return;
      }
      var deviceId = selectedDevice();
      if (!payload.template || !deviceId) {
        statusEl.textContent = "Choose device and template first.";
        return;
      }

      statusEl.textContent = "Generating diff...";
      outputEl.textContent = "";
      fetch("/api/devices/" + deviceId + "/diff", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "same-origin",
        body: JSON.stringify({
          template: payload.template,
          variables: payload.variables,
          allow_risky_commands: payload.allow_risky_commands
        })
      })
        .then(function (response) {
          return readJsonSafe(response).then(function (data) {
            return { ok: response.ok, status: response.status, data: data };
          });
        })
        .then(function (res) {
          if (!res.ok) {
            statusEl.textContent = "Diff failed (HTTP " + res.status + ").";
            outputEl.textContent = formatApiError(res.data, "Unknown error");
            return;
          }
          statusEl.textContent = "Diff ready.";
          outputEl.textContent = res.data.diff || "(no diff)";
        })
        .catch(function (err) {
          statusEl.textContent = "Diff failed.";
          outputEl.textContent = String(err);
        });
    });

    if (applyBtn) {
      applyBtn.addEventListener("click", function () {
        var payload;
        try {
          payload = buildPayload();
        } catch (err) {
          statusEl.textContent = "Apply job queue failed.";
          outputEl.textContent = String(err.message || err);
          return;
        }
        var deviceId = selectedDevice();
        if (!payload.template || !deviceId) {
          statusEl.textContent = "Choose device and template first.";
          return;
        }

        statusEl.textContent = "Queueing apply-template job...";
        outputEl.textContent = "";
        fetch("/api/devices/" + deviceId + "/apply_template", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          credentials: "same-origin",
          body: JSON.stringify(payload)
        })
          .then(function (response) {
            return readJsonSafe(response).then(function (data) {
              return { ok: response.ok, status: response.status, data: data };
            });
          })
          .then(function (res) {
            if (!res.ok) {
              statusEl.textContent = "Apply job queue failed (HTTP " + res.status + ").";
              var details = formatApiError(res.data, "Unknown error");
              outputEl.textContent = details;
              return;
            }
            var job = res.data.job || {};
            statusEl.textContent = "Job queued successfully.";
            outputEl.textContent = "Job #" + job.id + " queued.\nView: /jobs/" + job.id;
            if (jobLinkEl && job.id) {
              jobLinkEl.innerHTML = '<a class="btn-link" href="/jobs/' + job.id + '">Open Job #' + job.id + "</a>";
            }
          })
          .catch(function (err) {
            statusEl.textContent = "Apply job queue failed.";
            outputEl.textContent = String(err);
          });
      });
    }
  }

  function initJobsCreateForm() {
    var form = document.getElementById("job-create-form");
    if (!form) {
      return;
    }

    var model = window.NETOPS_JOB_FORM_DATA || { devices: [], templates: [], templateSchemas: {} };
    var devicesEl = document.getElementById("job-devices");
    var templateEl = document.getElementById("job-template");
    var fieldsEl = document.getElementById("job-template-fields");
    var allowRisky = document.getElementById("job-allow-risky");
    var statusEl = document.getElementById("job-create-status");
    var submitBtn = document.getElementById("job-create-submit");
    var endpoint = form.getAttribute("data-endpoint") || "/api/jobs";

    (model.devices || []).forEach(function (d) {
      var opt = document.createElement("option");
      opt.value = String(d.id);
      opt.textContent = d.name + " (" + d.ip_address + ")";
      devicesEl.appendChild(opt);
    });

    function syncTemplateFields() {
      var name = (templateEl.value || "").trim();
      var schema = (model.templateSchemas || {})[name] || { fields: [] };
      renderDynamicFields(fieldsEl, schema);
    }
    templateEl.addEventListener("change", syncTemplateFields);
    syncTemplateFields();

    form.addEventListener("submit", function (event) {
      event.preventDefault();

      var selectedDeviceIds = Array.from(devicesEl.selectedOptions || [])
        .map(function (opt) { return Number(opt.value); })
        .filter(function (v) { return Number.isInteger(v) && v > 0; });
      var templateName = (templateEl.value || "").trim();
      if (!selectedDeviceIds.length || !templateName) {
        statusEl.textContent = "At least one device and a template are required.";
        return;
      }

      var payload = {};
      try {
        payload = {
          type: "apply_template",
          device_ids: selectedDeviceIds,
          payload: {
            template: templateName,
            variables: buildVariablesFromFields(fieldsEl),
            allow_risky_commands: !!(allowRisky && allowRisky.checked)
          }
        };
      } catch (err) {
        statusEl.textContent = String(err.message || err);
        return;
      }

      submitBtn.disabled = true;
      statusEl.textContent = "Queueing job...";

      fetch(endpoint, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "same-origin",
        body: JSON.stringify(payload)
      })
        .then(function (response) {
          return readJsonSafe(response).then(function (data) {
            return { ok: response.ok, status: response.status, data: data };
          });
        })
        .then(function (res) {
          if (!res.ok) {
            statusEl.textContent = "Failed to queue job: " + ((res.data && res.data.error) || res.status);
            return;
          }
          var job = res.data.job || {};
          statusEl.textContent = "Job queued: #" + job.id;
          if (job.id) {
            window.location.href = "/jobs/" + job.id;
          }
        })
        .catch(function (err) {
          statusEl.textContent = "Request failed: " + err;
        })
        .finally(function () {
          submitBtn.disabled = false;
        });
    });
  }

  function initCaptureSnapshotButton() {
    var btn = document.getElementById("capture-snapshot-btn");
    if (!btn) {
      return;
    }

    var statusEl = document.getElementById("capture-status");
    var endpoint = btn.getAttribute("data-endpoint");

    btn.addEventListener("click", function () {
      btn.disabled = true;
      statusEl.textContent = "Requesting snapshot capture job...";
      statusEl.className = "text-info mt-2 small";

      fetch(endpoint, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "same-origin"
      })
        .then(function (response) {
          return readJsonSafe(response).then(function (data) {
            return { ok: response.ok, status: response.status, data: data };
          });
        })
        .then(function (res) {
          if (!res.ok || !res.data.success) {
            statusEl.textContent = "Error: " + ((res.data && res.data.error) || "Failed to start snapshot job.");
            statusEl.className = "text-danger mt-2 small";
            return;
          }
          var jobId = res.data.job_id;
          statusEl.innerHTML = '<span class="text-success">✓ Job #' + jobId + ' started.</span> <a href="/jobs/' + jobId + '" class="btn-link ms-2">View Job Status</a>';
          statusEl.className = "text-success mt-2 small";
        })
        .catch(function (err) {
          statusEl.textContent = "Request failed: " + String(err);
          statusEl.className = "text-danger mt-2 small";
        })
        .finally(function () {
          btn.disabled = false;
        });
    });
  }

  function initRollbackButton() {
    var btn = document.getElementById("rollback-latest-btn");
    if (!btn) {
      return;
    }

    var statusEl = document.getElementById("rollback-status");
    var jobsLinkEl = document.getElementById("rollback-jobs-link");
    var outputEl = document.getElementById("rollback-output");
    var endpoint = btn.getAttribute("data-endpoint");

    btn.addEventListener("click", function () {
      btn.disabled = true;
      statusEl.textContent = "Starting rollback...";
      if (jobsLinkEl) {
        jobsLinkEl.textContent = "";
      }
      outputEl.textContent = "";

      fetch(endpoint, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "same-origin"
      })
        .then(function (response) {
          return readJsonSafe(response).then(function (data) {
            return { ok: response.ok, status: response.status, data: data };
          });
        })
        .then(function (res) {
          if (!res.ok || !res.data.success) {
            statusEl.textContent = "Rollback failed.";
            outputEl.textContent = (res.data && res.data.error) || "Unknown error";
            return;
          }
          statusEl.textContent = "Rollback successful.";
          outputEl.textContent = res.data.rollback_output || "(no output)";
          if (jobsLinkEl) {
            jobsLinkEl.innerHTML = '<a class="btn-link" href="/jobs">View Jobs</a>';
          }
        })
        .catch(function (err) {
          statusEl.textContent = "Rollback request failed.";
          outputEl.textContent = String(err);
        })
        .finally(function () {
          btn.disabled = false;
        });
    });
  }

  function initTerminalModal() {
    var modal = document.getElementById("terminalModal");
    if (!modal) {
      return;
    }

    var titleEl = document.getElementById("terminalModalTitle");
    var bodyEl = document.getElementById("terminal-output-content");

    modal.addEventListener("show.bs.modal", function (event) {
      var trigger = event.relatedTarget;
      if (!trigger) {
        return;
      }

      if (trigger.classList.contains("terminal-trigger")) {
        titleEl.textContent = trigger.getAttribute("data-terminal-title") || "Terminal View";
        bodyEl.textContent = trigger.getAttribute("data-terminal-body") || "No output selected.";
      }

      if (trigger.classList.contains("connection-test-btn")) {
        var deviceId = trigger.getAttribute("data-device-id");
        var deviceName = trigger.getAttribute("data-device-name") || "Device";
        
        // Update title
        if (titleEl) titleEl.textContent = "Testing Connection: " + deviceName;
        
        // 1. Reset UI immediately with loading message
        if (bodyEl) {
          bodyEl.textContent = ">>> Establishing SSH session with " + deviceName + "...\n>>> Please wait (10-15 seconds)...";
          bodyEl.style.color = "#00ff00"; // Reset to green
        }

        // 2. Execute the API call
        fetch("/api/devices/" + deviceId + "/test-connection", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          credentials: "same-origin"
        })
          .then(function (response) {
            return readJsonSafe(response).then(function (data) {
              return { ok: response.ok, status: response.status, data: data };
            });
          })
          .then(function (result) {
            if (bodyEl) {
              if (result.ok && result.data && result.data.success) {
                bodyEl.textContent = result.data.output || "Connection successful! No output returned.";
                bodyEl.style.color = "#00ff00"; // Green for success
              } else {
                bodyEl.style.color = "#ff4444"; // Red for error
                var errorMessage = formatApiError(result.data, "Connection test failed.");
                bodyEl.textContent = "CONNECTION FAILED:\n" + errorMessage;
              }
            }
          })
          .catch(function (err) {
            if (bodyEl) {
              bodyEl.style.color = "#ff4444"; // Red for error
              bodyEl.textContent = "SYSTEM ERROR:\nCould not reach the automation service.\n" + String(err);
            }
          });
      }

      if (trigger.classList.contains("snapshot-compare-trigger")) {
        var compareEndpoint = trigger.getAttribute("data-compare-endpoint");
        titleEl.textContent = trigger.getAttribute("data-terminal-title") || "Snapshot Diff";
        bodyEl.textContent = "Generating configuration diff...";

        fetch(compareEndpoint, {
          method: "GET",
          headers: { "Accept": "application/json" },
          credentials: "same-origin"
        })
          .then(function (response) {
            return readJsonSafe(response).then(function (data) {
              return { ok: response.ok, status: response.status, data: data };
            });
          })
          .then(function (result) {
            if (result.ok && result.data && result.data.success) {
              bodyEl.textContent = result.data.diff || "No diff output generated.";
              return;
            }
            bodyEl.textContent = "Diff request failed (HTTP " + result.status + ").\n\n" + formatApiError(result.data, "Unable to compare snapshots.");
          })
          .catch(function (err) {
            bodyEl.textContent = "Diff request failed.\n\n" + String(err);
          });
      }
    });
  }

  initCommandRunner();
  initJobDetailPolling();
  initTemplatesTool();
  initJobsCreateForm();
  initCaptureSnapshotButton();
  initRollbackButton();
  initTerminalModal();
})();
