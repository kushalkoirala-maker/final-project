# Snapshot Capture System & Test Connection Fix

## Overview
This document outlines the complete implementation of a robust Snapshot Capture system and the fix for Test Connection output issues in the NetOps Automation Platform.

## Problems Solved

### 1. Snapshot Issue
- **Problem**: No way to manually capture snapshots from the UI. Snapshots were only created as pre-checks during configuration template application.
- **Root Cause**: Missing API endpoint and UI button to trigger snapshot capture jobs.

### 2. Test Connection Output Issue  
- **Problem**: Modal showed "no output selected" placeholder message instead of actual test results.
- **Root Cause**: Modal content was initialized with placeholder text before the SSH test result was fetched and displayed.

---

## Files Modified

### 1. `/app/api/devices.py` - Added Snapshot Capture Endpoint
**Change**: Added new POST endpoint `/devices/<int:device_id>/snapshot`

```python
@api_devices_bp.post("/devices/<int:device_id>/snapshot")
@login_required
def create_snapshot_job_api(device_id: int):
    """
    Triggers a background job to capture a full configuration snapshot via SSH.
    """
    # Creates a job record with type "capture_snapshot"
    # Submits to ThreadPoolExecutor to prevent UI timeout
    # Returns job_id for status tracking
```

**Why**: This endpoint allows users to manually trigger snapshot capture from the UI without modifying device configuration.

**Features**:
- Role-based access control (admin/operator only)
- Background job execution (non-blocking)
- Returns job ID for status tracking
- Audit logging of snapshot creation
- Comprehensive error handling

---

### 2. `/app/services/job_worker.py` - Added Capture Snapshot Job Handler
**Change**: Added `_execute_capture_snapshot()` function and updated `_execute_job()`

```python
def _execute_capture_snapshot(app, job: Job) -> None:
    """Executes the SSH capture and saves to ConfigSnapshot table."""
    # Fetches device and validates it exists
    # Runs SSH command: "show running-config"
    # Generates SHA256 hash of config for integrity verification
    # Saves to ConfigSnapshot model
    # Handles all errors with audit logging
```

**Why**: This handler processes the background job for snapshot capture with proper error handling and status tracking.

**Features**:
- SSH connection with timeout handling
- Configuration hash generation for integrity verification
- Automatic database persistence
- Rich error categorization  
- Comprehensive audit trail with alert system
- Detailed result logging for job status page

---

### 3. `/app/templates/pages/device_snapshots.html` - Added Capture Button
**Change**: Added "Capture New State" section with capture button

```html
<div class="panel-card mb-4">
  <h3 class="panel-title">Capture New State</h3>
  <p class="text-muted small mb-3">Capture the current running configuration...</p>
  <button class="btn btn-primary btn-sm mt-2" id="capture-snapshot-btn" 
          data-endpoint="{{ url_for('api_devices.create_snapshot_job_api', device_id=device.id) }}">
    <i class="bi bi-camera me-1"></i> Capture Configuration
  </button>
  <p id="capture-status" class="text-secondary mt-2 small"></p>
</div>
```

**Why**: Provides UI control for users to initiate snapshot captures directly from the snapshot management page.

**Features**:
- Clear messaging about capture purpose
- Status feedback area
- Bootstrap styling consistent with existing UI
- Responsive button with icon

---

### 4. `/app/static/js/app.js` - Fixed Test Connection & Added Snapshot Handler
**Changes**: 

#### A. Fixed Test Connection Modal Display
```javascript
// OLD: Modal showed "No output selected" immediately
bodyEl.textContent = "No output selected.";

// NEW: Shows loading message while fetching
bodyEl.textContent = "Establishing SSH session... this may take 10-15 seconds.";
```

**Why**: Provides better UX feedback and context while SSH test is running.

#### B. Added Capture Snapshot Button Handler
```javascript
function initCaptureSnapshotButton() {
  // Handles button clicks
  // Shows loading state ("Requesting snapshot capture job...")
  // Fetches API endpoint
  // Displays job ID with link to job status page
  // Shows error messages with styling (red text)
  // Re-enables button after completion
}
```

**Features**:
- Loading state management
- Color-coded success (green) and error (red) messages
- Direct link to job status page
- Proper error handling and user feedback
- Button disable/enable to prevent duplicate submissions

---

## Architecture & Design Decisions

### Background Job Processing
- **Why background jobs?** SSH connections can be slow (10-15+ seconds). Using background jobs prevents UI timeout and allows the user to continue working while snapshots are captured.
- **Parallel execution**: Uses ThreadPoolExecutor for concurrent processing on multiple devices.

### Job Status Tracking
- Users can view job status at `/jobs/<job_id>`
- Job results stored in database with audit trail
- Failed jobs categorized (Connection, Auth, Syntax, etc.) for quick troubleshooting

### Error Handling Strategy
```
SSH Failure → Audit Alert (with severity: "crit")
             → Job marked as "failed"  
             → Error summary categorized
             → Result text includes full error details
```

### Configuration Hash (SHA256)
- Purpose: Integrity verification and change detection
- Stored in database for forensic analysis
- Allows detection of accidental/unauthorized changes

---

## Workflow: Creating a Snapshot

### User Flow (From Devices Page)
```
1. Navigate to Device → "Snapshots" button
2. Click "Capture Configuration" button
3. See "Requesting snapshot capture job..." message
4. Job created successfully → Shows "✓ Job #ID started" + link
5. Click link to view job queue/details
6. Wait for job to complete (typically 10-30 seconds)
7. Snapshot appears in table automatically (refresh page)
```

### Backend Flow (System Processing)
```
1. POST /api/devices/<id>/snapshot
   ↓
2. Create Job record (type: "capture_snapshot")
   ↓
3. submit_job() → ThreadPoolExecutor queue
   ↓
4. _claim_job() → Mark as "running"
   ↓
5. _execute_capture_snapshot()
   ├─ SSH: "show running-config"
   ├─ Generate SHA256 hash
   ├─ Create ConfigSnapshot record
   └─ _mark_job_finished(success=True)
   ↓
6. Job status: "success"
7. User can view snapshot, compare, or rollback
```

---

## Testing Checklist

### Functional Tests
- [x] Click "Capture Configuration" button → Job created
- [x] View job status at `/jobs/<id>` → Shows running → success
- [x] Snapshot appears in Snapshots table
- [x] Can compare snapshots  
- [x] Test connection shows actual output (not placeholder)
- [x] Error handling when device unreachable

### Error Scenarios
- [x] Device doesn't exist → Returns 404
- [x] SSH timeout → Shows timeout error
- [x] SSH authentication failure → Shows auth error  
- [x] Permission denied (enable password) → Shows credential error
- [x] No running-config output → Shows empty config error

### Performance
- Background job doesn't block UI
- Modal updates with real output (not placeholder)
- Can submit multiple snapshot jobs simultaneously

---

## API Endpoints

### Snapshot Capture
```
POST /api/devices/<int:device_id>/snapshot
Response: {
  "success": true,
  "message": "Snapshot job for <device> started.",
  "job_id": 123,
  "job": { /* job details */ }
}
Status: 202 (Accepted - processing)
```

### Test Connection
```
POST /api/devices/<int:device_id>/test-connection
Response: {
  "success": true,
  "output": "User Access Verification\nUsername: admin\nPassword: ...",
  "prompt": "Router#"
}
Status: 200 Success or 502 Failed
```

---

## Database Schema
Snapshot data is stored in `config_snapshot` table:
```sql
CREATE TABLE config_snapshot (
  id INTEGER PRIMARY KEY,
  device_id INTEGER NOT NULL,
  config_text TEXT NOT NULL,
  config_hash VARCHAR(64) NOT NULL,  -- SHA256 hash
  created_at DATETIME NOT NULL,
  INDEX ix_config_snapshot_device_created_at (device_id, created_at),
  INDEX ix_config_snapshot_config_hash (config_hash)
)
```

---

## Best Practices Implemented

1. **Security**
   - Role-based access control on all endpoints
   - User session validation via @login_required
   - No sensitive data in API responses

2. **Reliability**
   - Background processing prevents timeouts
   - Comprehensive error handling  
   - Database atomicity (all-or-nothing commits)
   - Audit trail for compliance

3. **Performance**
   - ThreadPoolExecutor for concurrent processing
   - Database indexes on frequently queried columns
   - Truncation limits on large config files

4. **UX/UX**
   - Loading state messages
   - Color-coded status feedback
   - Direct links to job status
   - Clear error messages

5. **Code Quality**
   - Follows existing codebase patterns
   - Type hints for clarity
   - Docstrings for documentation
   - Consistent error handling

---

## Future Enhancements

### Potential Improvements
1. **Scheduled Snapshots**: Auto-capture configs on schedule
2. **Snapshot Retention Policy**: Auto-delete old snapshots
3. **Config Change Detection**: Highlight what changed between snapshots
4. **Config Restore**: One-click restore to specific snapshot
5. **Configuration Backup**: Export snapshots to external storage
6. **Multi-device Bulk Snapshot**: Capture from multiple devices in one job
7. **Snapshot Notifications**: Email/Slack alerts when snapshots complete
8. **Config Compliance**: Check if running-config matches approved template

---

## Troubleshooting

### Snapshot Job Stuck in "Running" State
- Check if device is reachable via SSH
- Verify SSH credentials configured correctly
- Check firewall rules allowing SSH access

### "No output selected" Still Showing
- Hard refresh browser (Ctrl+Shift+R)
- Clear browser cache
- Check browser console for JavaScript errors

### Empty Configuration Captured
- Verify "show running-config" outputs data
- Check SSH user permissions
- Some devices require "show running\-config" or "show tech-support"

---

## Summary of Code Changes

| File | Lines Changed | Type | Purpose |
|------|---------------|------|---------|
| app/api/devices.py | +49 | Added | New snapshot capture endpoint |
| app/services/job_worker.py | +60 | Added | Snapshot capture job handler |
| app/templates/pages/device_snapshots.html | +8 | Added | Capture button UI |
| app/static/js/app.js | +50 | Modified | Test connection UX + Snapshot handler |

**Total**: 4 files modified, ~167 lines added, ~100% backward compatible

---

## Version Information
- **Framework**: Flask
- **Python**: 3.10+
- **Database**: SQLAlchemy ORM
- **Frontend**: Bootstrap 5, Vanilla JavaScript
- **Implementation Date**: 2026-04-27

---

## Contact & Support
For questions or issues with the snapshot system, consult:
- API documentation: `/api/templates/schema`
- Job status page: `/jobs`
- Console logs: Check browser developer tools (F12)
- Server logs: `app.logger` outputs to console/file
