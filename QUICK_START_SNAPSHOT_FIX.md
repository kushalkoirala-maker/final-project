# Quick Implementation Guide - Snapshot & Test Connection Fix

## What Was Fixed

### 1. ✅ Snapshot Capture System (NEW)
**Problem**: No way to manually capture device snapshots  
**Solution**: Added background job system for snapshot capture

### 2. ✅ Test Connection Output (FIXED)
**Problem**: Modal showed "no output selected" instead of actual test results  
**Solution**: Improved modal display logic to show actual SSH output

---

## Files Modified (4 Total)

### File 1: `app/api/devices.py`
**Added**: POST endpoint `/devices/<id>/snapshot`
- Creates background job to capture config
- Returns job ID for tracking
- Status: 202 (Accepted)

### File 2: `app/services/job_worker.py`  
**Added**: `_execute_capture_snapshot()` handler
- Executes SSH command on device
- Generates SHA256 hash of config
- Saves to ConfigSnapshot table
- Handles errors with audit logging

### File 3: `app/templates/pages/device_snapshots.html`
**Added**: "Capture New State" section
- New button: "Capture Configuration"
- Status display area
- Help text explaining feature

### File 4: `app/static/js/app.js`
**Modified**: 
- Fixed test connection modal message (was "No output selected")
- Added snapshot capture button handler
- Shows job ID with status link

---

## How to Use

### Capturing a Snapshot
1. Navigate to a device → click "Snapshots" button
2. Click **"Capture Configuration"** button
3. Wait for "Job #X started" message
4. Click link to view job status (or refresh page in ~15 seconds)
5. Snapshot appears in table when complete

### Job Status Page
- Shows: Job ID, status (pending/running/success/failed), timing
- Per-device results with error details
- Auto-refreshes every 2 seconds

### Test Device Connection
- Now shows actual SSH output (not placeholder text)
- Displays prompt received from device
- Shows detailed error messages if connection fails

---

## API Endpoints

### Capture Snapshot
```
POST /api/devices/123/snapshot
Response: {
  "success": true,
  "message": "Snapshot job for router1 started.",
  "job_id": 456,
  "job": { /* job details */ }
}
```

### List Snapshots
```
GET /api/devices/123/snapshots
Returns: Array of snapshot objects with ID, hash, created_at
```

### Get Snapshot Detail
```
GET /api/snapshots/789
Returns: Full config text, hash, timestamps
```

---

## Error Handling

### Device Unreachable
```
Status: 502 Bad Gateway
Response: {
  "error": "Connection timeout - device unreachable"
}
```

### SSH Authentication Failed
```
Status: 202 (Job created, but will fail)
Job Result: "Auth failure - check enable password"
```

### Permission Denied
```
Job Result: "Insufficient privileges for command"
```

---

## Database Schema

### ConfigSnapshot Table
```
id (Primary Key)
device_id (Foreign Key)
config_text (Full configuration)
config_hash (SHA256 - for integrity)
created_at (Timestamp)

Indexes:
- (device_id, created_at)
- (config_hash)
```

### Job Table Updates
- Job type can now be: "apply_template" or "capture_snapshot"
- Result tracking same as other job types

---

## Testing Steps

### Verify Snapshot Capture
1. ✅ Click capture button
2. ✅ Verify job appears at /jobs/
3. ✅ Wait for job to complete
4. ✅ Verify snapshot in list with timestamp
5. ✅ Click "View" to see full config
6. ✅ Try "Compare" to see diff from previous

### Verify Test Connection
1. ✅ On Devices page, click "Test Connection"
2. ✅ Modal opens with message: "Establishing SSH session..."
3. ✅ Wait for SSH output to appear (not "No output selected")
4. ✅ Verify you see device prompt (e.g., "Router#")

### Error Cases
1. ✅ Stop device SSH → See "Connection refused" error
2. ✅ Wrong credentials → See "Permission denied" error
3. ✅ Use non-existent device ID → See "device not found" 404

---

## Performance Notes

- **Background Processing**: SSH operations run in background thread pool (max 4 workers)
- **Expected Duration**: 10-30 seconds per device
- **UI Responsiveness**: Non-blocking - users can continue working
- **Scalability**: Up to 4 concurrent snapshot jobs

---

## Security Features

✅ **Authorization**: Admin/Operator roles only  
✅ **Authentication**: Session validation  
✅ **Audit Trail**: All snapshots logged  
✅ **Data Integrity**: SHA256 hash verification  
✅ **Error Containment**: No sensitive data in logs  

---

## Rollback & Recovery

### If Device Config Gets Corrupted
1. Go to Device → Snapshots
2. Click "Rollback to Latest Snapshot"
3. System restores to previous known good state
4. Pre-rollback snapshot created for audit

---

## Files to Upload for Support

Provide these files if you need help debugging:

1. **Complete Files**: 
   - `app/api/devices.py` (entire file)
   - `app/services/job_worker.py` (entire file)
   - `app/templates/pages/device_snapshots.html` (entire file)
   - `app/static/js/app.js` (entire file)

2. **Logs** (if issues occur):
   - Flask application logs
   - Browser console (F12 → Console tab)
   - Database query logs (SQLAlchemy echo)

3. **Environment**:
   - Python version (should be 3.10+)
   - Flask version
   - SQLAlchemy version

---

## Next Steps / Future Enhancements

### Planned Features
- [ ] Scheduled automatic snapshots
- [ ] Config change detection (visual diff highlighting)
- [ ] Bulk snapshot all devices
- [ ] Export snapshots as files
- [ ] Snapshot retention policies
- [ ] Config compliance checking

### For Production Deployment
- [ ] Test with large config files (>1MB)
- [ ] Test with high device count (>100 devices)
- [ ] Monitor thread pool resource usage
- [ ] Set up snapshot retention cleanup job
- [ ] Configure backup export to external storage

---

## Troubleshooting

### Issue: "Job stuck in Running"
**Solution**: Check device connectivity, verify credentials, check logs

### Issue: "Still seeing 'No output selected'"
**Solution**: Clear browser cache (Ctrl+Shift+R), restart browser

### Issue: Snapshot capture timeout
**Solution**: Verify SSH timeout is set to 20+ seconds in config

### Issue: Empty snapshot captured
**Solution**: Test manual SSH - some Cisco devices need `show running\-config` with escaping

---

## Success Indicators ✅

You'll know it's working when:
- ✅ "Capture Configuration" button appears on snapshot page
- ✅ Click button → Job number returned immediately  
- ✅ Job page shows "running" then "success"
- ✅ Snapshot appears in list with current timestamp
- ✅ Test connection shows actual device output (not placeholder)
- ✅ Can view and compare snapshots

---

## Support & Documentation

- **API Docs**: Auto-generated at `/api/templates/schema`
- **Job Status**: `/jobs/<job_id>`
- **Config Files**: Check `SNAPSHOT_AND_CONNECTION_FIX.md` for detailed docs
- **Logs**: Check Flask app logger output

---

**Status**: ✅ COMPLETE - Ready for Testing

Last Updated: 2026-04-27
