bl_info = {
    "name": "SVG to 3D - Layers",
    "author": "Daniel Marcin - 3D & Mockups",
    "version": (1, 0, 0),
    "blender": (4, 5, 0),
    "location": "3D Viewport > Sidebar > SVG Layers Tab",
    "description": "Process SVG files with layer-based operations",
    "category": "Import-Export",
}

import bpy
import re
import json
import time
import math
import bmesh
import subprocess
import zipfile
import tempfile
import shutil
import os
import bpy.app.handlers
from pathlib import Path
from mathutils import Vector
from bpy.props import (
    StringProperty, 
    PointerProperty, 
    FloatProperty,
    FloatVectorProperty,
    IntProperty,
    BoolProperty,
    CollectionProperty,
    EnumProperty
)
from bpy.types import Panel, Operator, PropertyGroup
from bpy_extras.io_utils import ExportHelper
# Global flag to prevent recursive updates during sync
_sync_in_progress = False

def cleanup_mof_and_temp_files():
    """Clean up any leftover MOF processes and temp files"""
    print("[CLEANUP] Starting MOF and temp file cleanup...")
    import subprocess
    import tempfile
    from pathlib import Path
    
    # Kill any running MOF processes (Windows)
    try:
        result = subprocess.run(['taskkill', '/F', '/IM', 'UnwrapConsole3.exe'], 
                      capture_output=True, text=True)
        if "SUCCESS" in result.stdout:
            print("[CLEANUP] Killed MOF process")
    except:
        pass
    
    # Clean up temp OBJ files - MORE THOROUGH
    temp_dir = tempfile.gettempdir()
    cleaned_count = 0
    for pattern in ["Layer_*.obj", "Layer_*_unwrapped.obj", "*Layer*.obj"]:
        for file in Path(temp_dir).glob(pattern):
            try:
                file.unlink()
                cleaned_count += 1
            except:
                pass
    if cleaned_count > 0:
        print(f"[CLEANUP] Removed {cleaned_count} temp files")
    
    # Clean up scene properties
    scene = bpy.context.scene
    keys_to_remove = []
    for key in scene.keys():
        if 'mof' in key.lower() or '_temp_batch' in key:
            keys_to_remove.append(key)
    for key in keys_to_remove:
        del scene[key]
    # Clean UV data from layer objects
    for obj in bpy.data.objects:
        if obj.name.startswith("Layer") and obj.type == 'MESH':
            # Clear all UV layers from the original objects
            while obj.data.uv_layers:
                obj.data.uv_layers.remove(obj.data.uv_layers[0])
            print(f"[CLEANUP] Cleared UV data from {obj.name}")
    print("[CLEANUP] Cleanup complete")

def merge_by_distance_bmesh(obj, threshold=0.000001):
    import bmesh
    me = obj.data
    bm = bmesh.new()
    bm.from_mesh(me)
    bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=threshold)
    bm.normal_update()
    bm.to_mesh(me)
    bm.free()
    me.update()

def on_depsgraph_update(scene):
    """Handler that expands layer when object is selected (has orange outline)"""
    if not hasattr(bpy.context, 'selected_objects'):
        return
    
    # Check if we have the addon properties
    if not hasattr(scene, 'svg_layers_props'):
        return
    
    props = scene.svg_layers_props
    # Skip during export operations
    if getattr(props, 'is_exporting', False):
        return
    
    # Initialize tracking - track what's SELECTED (orange outline)
    if not hasattr(on_depsgraph_update, 'last_selected'):
        on_depsgraph_update.last_selected = None
    
    # Get currently selected objects (with orange outline)
    selected_objects = bpy.context.selected_objects
    
    # Get the name of selected layer (if any)
    current_selected = None
    if selected_objects:
        for obj in selected_objects:
            # Check if this is one of our layers
            for layer_setting in props.layer_settings:
                if layer_setting.layer_name == obj.name:
                    current_selected = obj.name
                    break
            if current_selected:
                break
    
    # If selection changed
    if current_selected != on_depsgraph_update.last_selected:
        on_depsgraph_update.last_selected = current_selected
        
        # If we selected one of our layers, expand it
        if current_selected:
            for setting in props.layer_settings:
                setting.show_layer = (setting.layer_name == current_selected)
            
            # Force UI redraw
            for area in bpy.context.screen.areas:
                if area.type == 'VIEW_3D':
                    area.tag_redraw()


# Points redistribution functions from previous addon
def resample_curve(curve_obj, point_spacing, straight_removal=0, tolerance=0.0001):
    """
    Resample a curve with fixed spacing between points.
    Remove percentage of points on straight edges using the collinear detection method.
    """
    
    if curve_obj.type != 'CURVE':
        return False
    
    curve_data = curve_obj.data
    
    # First pass: collect all spline data and calculate lengths
    splines_data = []
    
    for spline_idx, spline in enumerate(curve_data.splines):
        spline_info = {
            'cyclic': spline.use_cyclic_u,
            'points': [],
            'length': 0
        }
        
        # Get points based on spline type
        if spline.type == 'BEZIER':
            # Sample the bezier curve
            spline_info['points'] = sample_bezier_spline(spline, samples=50)
        
        elif spline.type == 'NURBS':
            # For NURBS, get the control points
            for point in spline.points:
                co = Vector(point.co[:3])  # Get xyz, ignore w
                spline_info['points'].append(co.copy())
        
        elif spline.type == 'POLY':
            # For poly splines, get the points directly
            for point in spline.points:
                co = Vector(point.co[:3])  # Get xyz, ignore w
                spline_info['points'].append(co.copy())
        
        if len(spline_info['points']) >= 2:
            # Calculate this spline's length
            spline_info['length'] = calculate_spline_length(
                spline_info['points'], 
                spline_info['cyclic']
            )
            
            # Calculate how many points this spline needs
            if spline_info['cyclic']:
                # For cyclic curves, points wrap around
                num_points = max(3, int(spline_info['length'] / point_spacing))
                spline_info['num_points'] = num_points
                spline_info['actual_spacing'] = spline_info['length'] / num_points
            else:
                # For open curves, we need at least 2 points
                num_segments = max(1, int(spline_info['length'] / point_spacing))
                spline_info['num_points'] = num_segments + 1
                spline_info['actual_spacing'] = spline_info['length'] / num_segments
            
            splines_data.append(spline_info)
    
    if not splines_data:
        return False
    
    # Clear all existing splines
    curve_data.splines.clear()
    
    # Second pass: create new splines with calculated points
    for spline_idx, spline_info in enumerate(splines_data):
        original_points = spline_info['points']
        is_cyclic = spline_info['cyclic']
        num_points = spline_info['num_points']
        actual_spacing = spline_info['actual_spacing']
        
        # Calculate segment lengths for accurate placement
        segments = []
        segment_lengths = []
        
        for i in range(len(original_points) - 1):
            length = (original_points[i + 1] - original_points[i]).length
            segments.append((original_points[i], original_points[i + 1]))
            segment_lengths.append(length)
        
        if is_cyclic:
            # Add closing segment
            length = (original_points[0] - original_points[-1]).length
            segments.append((original_points[-1], original_points[0]))
            segment_lengths.append(length)
        
        # Place new points at regular intervals
        new_points = []
        
        for i in range(num_points):
            target_distance = i * actual_spacing
            
            # Find which segment contains this distance
            current_distance = 0
            for seg_idx, (seg_length, (p1, p2)) in enumerate(zip(segment_lengths, segments)):
                if current_distance + seg_length >= target_distance:
                    # This segment contains our target point
                    local_distance = target_distance - current_distance
                    if seg_length > 0:
                        t = local_distance / seg_length
                        new_point = p1.lerp(p2, t)
                    else:
                        new_point = p1.copy()
                    new_points.append(new_point)
                    break
                current_distance += seg_length
            else:
                # If we didn't find a segment (shouldn't happen), use last point
                if original_points:
                    new_points.append(original_points[-1].copy())
        
        # Remove collinear points based on percentage
        if straight_removal > 0 and len(new_points) > 2:
            new_points = remove_collinear_points(new_points, is_cyclic, straight_removal, tolerance)
        
        # Create new poly spline
        if len(new_points) >= 2:
            new_spline = curve_data.splines.new('POLY')
            new_spline.use_cyclic_u = is_cyclic
            
            # Add points
            new_spline.points.add(len(new_points) - 1)
            
            # Set positions
            for i, pt in enumerate(new_points):
                new_spline.points[i].co = (pt.x, pt.y, pt.z, 1.0)
    
    # Update curve
    curve_data.update_tag()
    
    return True


def sample_bezier_spline(spline, samples=100):
    """Sample points along a bezier spline"""
    points = []
    bezier_points = spline.bezier_points
    
    if len(bezier_points) < 2:
        return points
    
    # For each segment between bezier points
    for i in range(len(bezier_points) - 1):
        p0 = bezier_points[i]
        p1 = bezier_points[i + 1]
        
        # Get control points
        start = p0.co
        handle1 = p0.handle_right
        handle2 = p1.handle_left
        end = p1.co
        
        # Sample along this bezier segment
        for j in range(samples):
            t = j / samples
            
            # Cubic bezier interpolation
            s = 1 - t
            point = (s**3 * start + 
                    3 * s**2 * t * handle1 + 
                    3 * s * t**2 * handle2 + 
                    t**3 * end)
            
            points.append(point.copy())
    
    # Add the last point
    points.append(bezier_points[-1].co.copy())
    
    # Handle cyclic curves
    if spline.use_cyclic_u:
        # Add segment from last to first
        p0 = bezier_points[-1]
        p1 = bezier_points[0]
        
        start = p0.co
        handle1 = p0.handle_right
        handle2 = p1.handle_left
        end = p1.co
        
        for j in range(samples):
            t = j / samples
            s = 1 - t
            point = (s**3 * start + 
                    3 * s**2 * t * handle1 + 
                    3 * s * t**2 * handle2 + 
                    t**3 * end)
            
            points.append(point.copy())
    
    return points


def calculate_spline_length(points, is_cyclic):
    """Calculate the total length of a spline from its points"""
    total_length = 0
    
    for i in range(len(points) - 1):
        total_length += (points[i + 1] - points[i]).length
    
    if is_cyclic and len(points) > 1:
        total_length += (points[0] - points[-1]).length
    
    return total_length


def remove_collinear_points(points, is_cyclic, removal_percentage=100, tolerance=0.0001):
    """Remove percentage of points that lie on straight lines between their neighbors"""
    if len(points) < 3 or removal_percentage == 0:
        return points
    
    # First, find continuous straight sections (not just individual collinear points)
    straight_sections = find_straight_sections(points, is_cyclic, tolerance)
    
    if not straight_sections:
        return points
    
    # Process each straight section
    final_points = []
    last_end = 0
    
    for section_start, section_end in straight_sections:
        # Add points before this straight section
        final_points.extend(points[last_end:section_start])
        
        # Get points in straight section (inclusive)
        section_points = points[section_start:section_end + 1]
        section_length = len(section_points)
        
        if section_length > 2:
            # Calculate how many points to keep
            points_to_remove = int(section_length * (removal_percentage / 100.0))
            points_to_keep = section_length - points_to_remove
            
            # Ensure we keep at least 2 points (start and end)
            if points_to_keep < 2:
                points_to_keep = 2
            
            if points_to_keep == section_length:
                # Keep all points
                final_points.extend(section_points)
            else:
                # Redistribute remaining points evenly along the straight line
                start_point = section_points[0]
                end_point = section_points[-1]
                
                # Add redistributed points
                for i in range(points_to_keep):
                    if i == 0:
                        final_points.append(start_point)
                    elif i == points_to_keep - 1:
                        final_points.append(end_point)
                    else:
                        # Interpolate position along straight line
                        t = i / (points_to_keep - 1)
                        interpolated_point = start_point.lerp(end_point, t)
                        final_points.append(interpolated_point)
        else:
            # Too few points to optimize
            final_points.extend(section_points)
        
        last_end = section_end + 1
    
    # Add remaining points after last straight section
    if last_end < len(points):
        final_points.extend(points[last_end:])
    
    # For cyclic curves, ensure we have at least 3 points
    if is_cyclic and len(final_points) < 3:
        # This shouldn't happen with proper straight section detection, but just in case
        return points[:3] if len(points) >= 3 else points
    
    return final_points


def find_straight_sections(points, is_cyclic, tolerance=0.0001):
    """Find continuous sections of straight lines"""
    if len(points) < 3:
        return []
    
    straight_sections = []
    in_straight = False
    straight_start = 0
    
    # Check each sequence of points
    for i in range(len(points) - 2):
        # Check if current point and its neighbors are collinear
        p1 = points[i]
        p2 = points[i + 1] 
        p3 = points[i + 2]
        
        vec1 = (p2 - p1).normalized()
        vec2 = (p3 - p2).normalized()
        dot = vec1.dot(vec2)
        
        is_straight = abs(abs(dot) - 1.0) < tolerance
        
        if is_straight:
            if not in_straight:
                # Start of a new straight section
                straight_start = i
                in_straight = True
        else:
            if in_straight:
                # End of straight section
                if i + 1 - straight_start >= 2:  # At least 3 points
                    straight_sections.append((straight_start, i + 1))
                in_straight = False
    
    # Handle section that extends to the end
    if in_straight:
        end_idx = len(points) - 1
        if end_idx - straight_start >= 2:
            straight_sections.append((straight_start, end_idx))
    
    # For cyclic curves, check if last and first sections connect
    if is_cyclic and len(points) > 3 and straight_sections:
        # Check if curve wraps around (last-first-second points are collinear)
        p1 = points[-2]
        p2 = points[-1]
        p3 = points[0]
        p4 = points[1]
        
        vec1 = (p2 - p1).normalized()
        vec2 = (p3 - p2).normalized()
        vec3 = (p4 - p3).normalized()
        
        if (abs(abs(vec1.dot(vec2)) - 1.0) < tolerance and 
            abs(abs(vec2.dot(vec3)) - 1.0) < tolerance):
            # The curve wraps around - merge first and last sections if needed
            if straight_sections[0][0] == 0 and straight_sections[-1][1] == len(points) - 1:
                # Merge first and last sections
                new_start = straight_sections[-1][0]
                new_end = straight_sections[0][1]
                # Remove the original sections and add the merged one
                if len(straight_sections) > 2:
                    middle_sections = straight_sections[1:-1]
                    straight_sections = [(new_start, len(points) - 1), (0, new_end)] + middle_sections
                else:
                    # The entire curve is straight
                    straight_sections = [(0, len(points) - 1)]
    
    return straight_sections


def store_curve_data(curve_obj):
    """Store the current state of a curve for later restoration"""
    if curve_obj.type != 'CURVE':
        return None
    
    curve_data = curve_obj.data
    stored_data = {
        'splines': []
    }
    
    for spline in curve_data.splines:
        spline_data = {
            'type': spline.type,
            'use_cyclic_u': spline.use_cyclic_u,
            'points': []
        }
        
        if spline.type == 'BEZIER':
            for point in spline.bezier_points:
                spline_data['points'].append({
                    'co': point.co.copy(),
                    'handle_left': point.handle_left.copy(),
                    'handle_right': point.handle_right.copy(),
                    'handle_left_type': point.handle_left_type,
                    'handle_right_type': point.handle_right_type
                })
        elif spline.type in ['POLY', 'NURBS']:
            for point in spline.points:
                spline_data['points'].append({
                    'co': Vector(point.co[:])
                })
        
        stored_data['splines'].append(spline_data)
    
    return stored_data


def restore_curve_data(curve_obj, stored_data):
    """Restore a curve to a previously stored state"""
    if curve_obj.type != 'CURVE' or not stored_data:
        return False
    
    curve_data = curve_obj.data
    
    # Clear existing splines
    curve_data.splines.clear()
    
    # Restore splines
    for spline_data in stored_data['splines']:
        new_spline = curve_data.splines.new(spline_data['type'])
        new_spline.use_cyclic_u = spline_data['use_cyclic_u']
        
        if spline_data['type'] == 'BEZIER':
            # Add bezier points
            new_spline.bezier_points.add(len(spline_data['points']) - 1)
            for i, point_data in enumerate(spline_data['points']):
                bp = new_spline.bezier_points[i]
                bp.co = point_data['co']
                bp.handle_left = point_data['handle_left']
                bp.handle_right = point_data['handle_right']
                bp.handle_left_type = point_data['handle_left_type']
                bp.handle_right_type = point_data['handle_right_type']
        
        elif spline_data['type'] in ['POLY', 'NURBS']:
            # Add poly/nurbs points
            new_spline.points.add(len(spline_data['points']) - 1)
            for i, point_data in enumerate(spline_data['points']):
                co = point_data['co']
                if len(co) == 3:
                    new_spline.points[i].co = (co.x, co.y, co.z, 1.0)
                else:
                    new_spline.points[i].co = co
    
    # Update curve
    curve_data.update_tag()
    
    return True

def apply_mof_unwrap(obj, layer_settings, exe_path=None):
    """Apply Ministry of Flat UV unwrapping to an object"""
    
    # Ensure we're in object mode
    if bpy.context.mode != 'OBJECT':
        bpy.ops.object.mode_set(mode='OBJECT')
    
    scene = bpy.context.scene
    
    # Handle MOF executable
    if exe_path and os.path.exists(exe_path):
        exe = exe_path
        extract_path = None  # No extraction needed since exe already exists
        # Use provided exe path from batch export
    else:
        cached = scene.get('_temp_batch_mof_exe')
        if cached and os.path.exists(cached):
            exe = cached
            extract_path = None  # do not clean cached folder here
        else:
            # Extract MOF for single export
            addon_dir = os.path.dirname(os.path.realpath(__file__))
            mof_zip_path = os.path.join(addon_dir, "resources", "MinistryOfFlat_Release.zip")
        
            if not os.path.exists(mof_zip_path):
                print("MinistryOfFlat_Release.zip not found in resources folder")
                return False
        
            try:
                extract_path = tempfile.mkdtemp(prefix="layers_mof_")
                with zipfile.ZipFile(mof_zip_path, 'r') as zip_ref:
                    zip_ref.extractall(extract_path)
            except Exception as e:
                print(f"Failed to extract MOF: {e}")
                return False
        
            exe = None
            for root, dirs, files in os.walk(extract_path):
                for file in files:
                    if file.lower() == "unwrapconsole3.exe":
                        exe = os.path.join(root, file)
                        break
                if exe:
                    break
            
            if not exe:
                print("MOF executable not found in zip")
                if extract_path:
                    shutil.rmtree(extract_path)
                return False
        
    scene['_temp_batch_mof_exe'] = exe 
    scene['_temp_batch_mof_extract'] = extract_path
    
    bpy.ops.object.select_all(action='DESELECT')
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    
    if layer_settings.mof_triangulate:
        bpy.ops.object.mode_set(mode='OBJECT')
        triang_mod = obj.modifiers.new(name="Triangulate", type='TRIANGULATE')
        triang_mod.min_vertices = 5
        triang_mod.keep_custom_normals = True
        bpy.ops.object.modifier_apply(modifier="Triangulate")
    
    if layer_settings.mof_separate_hard_edges:
        bpy.ops.object.mode_set(mode='EDIT')
        bm = bmesh.from_edit_mesh(obj.data)
        for edge in bm.edges:
            if not edge.smooth:
                edge.seam = True
        bmesh.update_edit_mesh(obj.data)
        bpy.ops.object.mode_set(mode='OBJECT')
    
    temp_dir = bpy.app.tempdir
    name_safe = obj.name.replace(" ", "_")
    in_path = os.path.join(temp_dir, f"{name_safe}.obj")
    out_path = os.path.join(temp_dir, f"{name_safe}_unwrapped.obj")
    
    try:
        bpy.ops.wm.obj_export(
            filepath=in_path,
            export_selected_objects=True,
            export_materials=False,
            forward_axis='Y',
            up_axis='Z'
        )
    except Exception as e:
        print(f"Export failed: {e}")
        if extract_path:
            shutil.rmtree(extract_path)
        return False
    
    cmd = [exe, in_path, out_path]
    params = [
        ("-RESOLUTION", str(layer_settings.texture_resolution)),
        ("-SEPARATE", "TRUE" if layer_settings.mof_separate_hard_edges else "FALSE"),
        ("-ASPECT", "1.0"),
        ("-NORMALS", "TRUE" if layer_settings.mof_use_normals else "FALSE"),
        ("-UDIMS", "1"),
        ("-OVERLAP", "TRUE" if layer_settings.mof_overlap_identical else "FALSE"),
        ("-MIRROR", "TRUE" if layer_settings.mof_overlap_mirrored else "FALSE"),
        ("-WORLDSCALE", "TRUE" if layer_settings.mof_world_scale else "FALSE"),
        ("-DENSITY", "1024"),
        ("-CENTER", "0.0", "0.0", "0.0"),
        ("-SUPRESS", "TRUE" if layer_settings.mof_suppress_validation else "FALSE"),
        ("-QUAD", "TRUE"),
        ("-WELD", "FALSE"),
        ("-FLAT", "TRUE"),
        ("-CONE", "TRUE"),
        ("-CONERATIO", "0.5"),
        ("-GRIDS", "TRUE"),
        ("-STRIP", "TRUE"),
        ("-PATCH", "TRUE"),
        ("-PLANES", "TRUE"),
        ("-FLATT", "0.9"),
        ("-MERGE", "TRUE"),
        ("-MERGELIMIT", "0.0"),
        ("-PRESMOOTH", "TRUE"),
        ("-SOFTUNFOLD", "TRUE"),
        ("-TUBES", "TRUE"),
        ("-JUNCTIONSDEBUG", "TRUE"),
        ("-EXTRADEBUG", "FALSE"),
        ("-ABF", "TRUE"),
        ("-SMOOTH", "TRUE" if layer_settings.mof_smooth else "FALSE"),
        ("-REPAIRSMOOTH", "TRUE"),
        ("-REPAIR", "TRUE"),
        ("-SQUARE", "TRUE"),
        ("-RELAX", "TRUE"),
        ("-RELAX_ITERATIONS", "30"),
        ("-EXPAND", "0.25"),
        ("-CUTDEBUG", "TRUE"),
        ("-STRETCH", "TRUE"),
        ("-MATCH", "TRUE"),
        ("-PACKING", "TRUE"),
        ("-RASTERIZATION", "32"),
        ("-PACKING_ITERATIONS", "1"),
        ("-SCALETOFIT", "0.5"),
        ("-VALIDATE", "FALSE"),
    ]
    for param in params:
        cmd.extend(param)
    
    try:
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.STDOUT
        )
        
        # Poll process with timeout
        timeout = 150.0 if exe_path else 150.0
        start_time = time.time()
        
        while True:
            # Check if process finished
            retcode = process.poll()
            if retcode is not None:
                # Process finished
                if retcode != 0 and not os.path.exists(out_path):
                    print(f"MOF failed with code: {retcode}")
                    if extract_path:
                        shutil.rmtree(extract_path)
                    return False
                break
            
            # Check timeout
            if time.time() - start_time > timeout:
                print(f"MOF timeout - killing process for {obj.name}")
                process.terminate()
                try:
                    process.wait(timeout=2)
                except:
                    process.kill()
                if extract_path:
                    shutil.rmtree(extract_path)
                return False
            
            # Check if batch export was cancelled (only if we're actually in batch processing)
            if exe_path and hasattr(bpy.context.scene.svg_layers_props, 'is_processing'):
                props = bpy.context.scene.svg_layers_props
                # Only kill MOF if is_processing was True and is now False (actual cancellation)
                # Don't kill if is_processing was never set (single export)
                if props.is_processing == False and props.progress_total > 0:
                    # Batch was actually cancelled, kill MOF
                    print("Batch cancelled - killing MOF process")
                    process.terminate()
                    try:
                        process.wait(timeout=2)
                    except:
                        process.kill()
                    if extract_path:
                        shutil.rmtree(extract_path, ignore_errors=True)
                    return False
            
            # Keep Blender UI responsive during long unwraps
            try:
                bpy.ops.wm.redraw_timer(type='DRAW_WIN_SWAP', iterations=1)
            except:
                pass
            time.sleep(0.05)  # small sleep to yield
        
        # If we get here, process completed successfully
        if not os.path.exists(out_path):
            print(f"MOF completed but output file not found")
            if extract_path:
                shutil.rmtree(extract_path)
            return False
                
    except Exception as e:
        print(f"Error running MOF: {e}")
        if extract_path:
            shutil.rmtree(extract_path)
        return False
    
    try:
        bpy.ops.wm.obj_import(filepath=out_path, forward_axis='Y', up_axis='Z')
    except Exception as e:
        print(f"Import failed: {e}")
        if extract_path:
            shutil.rmtree(extract_path)
        return False
    
    imported_obj = bpy.context.active_object
    if imported_obj and imported_obj.type == 'MESH':
        if not obj.data.uv_layers:
            obj.data.uv_layers.new()
        
        bpy.context.view_layer.objects.active = obj
        dt_mod = obj.modifiers.new(name="DataTransfer", type='DATA_TRANSFER')
        dt_mod.object = imported_obj
        dt_mod.use_loop_data = True
        dt_mod.data_types_loops = {'UV'}
        dt_mod.loop_mapping = 'TOPOLOGY'
        
        bpy.ops.object.modifier_apply(modifier=dt_mod.name)
        
        bpy.data.objects.remove(imported_obj, do_unlink=True)
        
        bpy.context.view_layer.objects.active = obj
        bpy.ops.object.mode_set(mode='EDIT')
        bpy.ops.mesh.select_all(action='SELECT')
        bpy.ops.uv.select_all(action='SELECT')
        
        bpy.ops.uv.average_islands_scale()
        bpy.ops.uv.pack_islands(margin=0.001)
        
        bpy.ops.object.mode_set(mode='OBJECT')
    
    for fp in (in_path, out_path):
        if os.path.exists(fp):
            try:
                os.remove(fp)
            except:
                pass
    
    # Only cleanup if this isn't the cached one
    if extract_path and bpy.context.scene.get('_temp_batch_mof_extract') != extract_path:
        try:
            shutil.rmtree(extract_path)
        except:
            pass
    
    return True

def get_or_extract_mof_exe(context):
    scn = context.scene

    if hasattr(context, "_batch_mof_exe") and context._batch_mof_exe and os.path.exists(context._batch_mof_exe):
        return context._batch_mof_exe

    p = scn.get('_temp_batch_mof_exe', None)
    if p and os.path.exists(p):
        return p

    p = scn.get('_temp_single_mof_exe', None)
    if p and os.path.exists(p):
        return p

    addon_dir = os.path.dirname(os.path.realpath(__file__))
    mof_zip_path = os.path.join(addon_dir, "resources", "MinistryOfFlat_Release.zip")
    extract_path = tempfile.mkdtemp(prefix="layers_mof_single_")

    with zipfile.ZipFile(mof_zip_path, 'r') as z:
        z.extractall(extract_path)

    exe = None
    for root, dirs, files in os.walk(extract_path):
        for f in files:
            if f.lower() == "unwrapconsole3.exe":
                exe = os.path.join(root, f)
                break
        if exe:
            break

    if exe:
        scn['_temp_single_mof_exe'] = exe
        scn['_temp_single_mof_dir'] = extract_path
        return exe

    shutil.rmtree(extract_path, ignore_errors=True)
    return None

def apply_uv_unwrap(obj, layer_settings, context):
    if not obj or obj.type != 'MESH':
        return False

    if bpy.context.mode != 'OBJECT':
        try:
            bpy.ops.object.mode_set(mode='OBJECT')
        except:
            pass

    try:
        bpy.ops.object.select_all(action='DESELECT')
    except:
        for o in bpy.data.objects:
            try:
                o.select_set(False)
            except:
                pass

    try:
        obj.select_set(True)
        context.view_layer.objects.active = obj
    except:
        return False

    if not obj.data.uv_layers:
        obj.data.uv_layers.new()

    method = getattr(layer_settings, "uv_method", "SMART")

    if method == 'SMART':
        bpy.ops.object.mode_set(mode='EDIT')
        bpy.ops.mesh.select_all(action='SELECT')
        bpy.ops.uv.smart_project(
            angle_limit=math.radians(layer_settings.uv_angle_limit),
            margin_method=layer_settings.uv_margin_method,
            rotate_method=layer_settings.uv_rotate_method,
            island_margin=layer_settings.uv_island_margin,
            area_weight=layer_settings.uv_area_weight,
            correct_aspect=layer_settings.uv_correct_aspect,
            scale_to_bounds=layer_settings.uv_scale_to_bounds
        )

    elif method == 'MOF':
        exe_path = getattr(bpy.context, '_batch_mof_exe', None) or bpy.context.scene.get('_temp_batch_mof_exe')
        if not exe_path:
            exe_path = context.scene.get('_temp_batch_mof_exe', None)
        if not exe_path:
            exe_path = get_or_extract_mof_exe(context)

        ok = apply_mof_unwrap(obj, layer_settings, exe_path)
        if not ok:
            return False

    elif method == 'CUBE':
        bpy.ops.object.mode_set(mode='OBJECT')
        max_dim = max(obj.dimensions) if max(obj.dimensions) > 0 else layer_settings.cube_size
        bpy.ops.object.mode_set(mode='EDIT')
        bpy.ops.uv.cube_project(
            cube_size=max_dim,
            correct_aspect=layer_settings.cube_correct_aspect,
            clip_to_bounds=layer_settings.cube_clip_to_bounds,
            scale_to_bounds=layer_settings.cube_scale_to_bounds
        )

    if getattr(layer_settings, "enable_uv_packing", False):
        if bpy.context.mode != 'EDIT_MESH':
            bpy.ops.object.mode_set(mode='EDIT')
            bpy.ops.mesh.select_all(action='SELECT')
        bpy.ops.uv.select_all(action='SELECT')
        if method == 'MOF':
            bpy.ops.uv.average_islands_scale()
        bpy.ops.uv.pack_islands(
            shape_method=layer_settings.pack_shape_method,
            scale=layer_settings.pack_scale,
            rotate=layer_settings.pack_rotate,
            margin_method=layer_settings.pack_margin_method,
            margin=layer_settings.pack_margin,
            pin=layer_settings.pack_pin_islands,
            pin_method=layer_settings.pack_pin_method,
            merge_overlap=layer_settings.pack_merge_overlapping,
            udim_source=layer_settings.pack_udim_source
        )

    bpy.ops.object.mode_set(mode='OBJECT')
    return True

def bake_textures_for_layer(obj, layer_settings, context):
    """Bake textures for a layer with AO enabled"""
    if not obj or not obj.data.materials:
        return False
    
    # Ensure we're in object mode before selection
    if context.mode != 'OBJECT':
        try:
            bpy.ops.object.mode_set(mode='OBJECT')
        except:
            pass
    
    mat = obj.data.materials[0]
    if not mat.use_nodes:
        return False
    
    nodes = mat.node_tree.nodes
    links = mat.node_tree.links
    
    # Find Principled BSDF
    principled = None
    material_output = None
    ao_image = None  # Track AO image for later use
    
    for node in nodes:
        if node.type == 'BSDF_PRINCIPLED':
            principled = node
        elif node.type == 'OUTPUT_MATERIAL':
            material_output = node
    
    if not principled or not material_output:
        return False
    
    # Store original render settings
    original_engine = context.scene.render.engine
    original_samples = context.scene.cycles.samples
    original_use_denoising = context.scene.cycles.use_denoising
    
    # Switch to Cycles for baking
    context.scene.render.engine = 'CYCLES'
    context.scene.cycles.samples = layer_settings.bake_samples
    context.scene.cycles.use_denoising = False
    
    # Configure bake settings
    context.scene.render.bake.margin = layer_settings.bake_margin
    
    # Select object for baking (safer method)
    try:
        bpy.ops.object.select_all(action='DESELECT')
    except:
        # If normal selection fails, deselect manually
        for o in bpy.data.objects:
            try:
                o.select_set(False)
            except:
                pass
    
    try:
        obj.select_set(True)
        context.view_layer.objects.active = obj
    except:
        print(f"[BAKE ERROR] Could not select {obj.name}")
        return False
    
    try:
        # Check if Base Color has a texture connected
        base_color_socket = principled.inputs['Base Color']
        should_bake_base_color = base_color_socket.is_linked

        if should_bake_base_color:
            # Create base color texture only if there's something to bake
            base_color_image = bpy.data.images.new(
                name=f"{obj.name}_BaseColor",
                width=layer_settings.texture_resolution,
                height=layer_settings.texture_resolution,
                alpha=False,
                float_buffer=False
            )
            base_color_image.colorspace_settings.name = 'sRGB'
            
            # Create image texture node for base color
            base_tex_node = nodes.new(type='ShaderNodeTexImage')
            base_tex_node.image = base_color_image
            base_tex_node.location = (principled.location.x - 400, principled.location.y)
            
            # Select the texture node for baking
            for node in nodes:
                node.select = False
            base_tex_node.select = True
            nodes.active = base_tex_node
            
            # Bake base color
            if layer_settings.bake_method == 'EMIT':
                # Store original connection
                original_surface_link = None
                for link in links:
                    if link.to_node == material_output and link.to_socket.name == 'Surface':
                        original_surface_link = link.from_socket
                        break
                
                # Temporarily disconnect and connect color directly to output
                for link in list(links):
                    if link.to_node == material_output:
                        links.remove(link)

                # Connect base color directly
                base_color_socket = principled.inputs['Base Color']
                if base_color_socket.is_linked:
                    for link in links:
                        if link.to_socket == base_color_socket:
                            links.new(link.from_socket, material_output.inputs['Surface'])
                            break
                
                # Bake emission
                bpy.ops.object.bake(type='EMIT')
                
                # Restore original connection
                if original_surface_link:
                    links.new(original_surface_link, material_output.inputs['Surface'])
            
            else:  # DIFFUSE method
                # Set up for diffuse baking
                context.scene.render.bake.use_pass_direct = True
                context.scene.render.bake.use_pass_indirect = True
                context.scene.render.bake.use_pass_color = True
                
                # Bake diffuse
                bpy.ops.object.bake(type='DIFFUSE')
            
            # Connect baked texture to Base Color
            links.new(base_tex_node.outputs['Color'], principled.inputs['Base Color'])

        # If Base Color is just a color value, skip baking and keep it as is
        # The Principled BSDF will use the color value directly in the GLB
        
        # Now bake other maps if they have connections
        
        # Bake Metallic if connected
        metallic_socket = principled.inputs['Metallic']
        if metallic_socket.is_linked:
            metallic_image = bpy.data.images.new(
                name=f"{obj.name}_Metallic",
                width=layer_settings.texture_resolution,
                height=layer_settings.texture_resolution,
                alpha=False,
                float_buffer=False
            )
            metallic_image.colorspace_settings.name = 'Non-Color'
            
            metallic_tex_node = nodes.new(type='ShaderNodeTexImage')
            metallic_tex_node.image = metallic_image
            metallic_tex_node.location = (principled.location.x - 400, principled.location.y - 300)
            
            # Select for baking
            for node in nodes:
                node.select = False
            metallic_tex_node.select = True
            nodes.active = metallic_tex_node
            
            # Get the node connected to metallic
            for link in links:
                if link.to_socket == metallic_socket:
                    # Disconnect material output and connect metallic source
                    for l in list(links):
                        if l.to_node == material_output:
                            links.remove(l)
                    links.new(link.from_socket, material_output.inputs['Surface'])
                    
                    # Bake
                    bpy.ops.object.bake(type='EMIT')
                    
                    # Restore connection
                    links.new(principled.outputs['BSDF'], material_output.inputs['Surface'])
                    
                    # Connect baked texture
                    links.new(metallic_tex_node.outputs['Color'], principled.inputs['Metallic'])
                    break
        
        # Bake Roughness if connected
        roughness_socket = principled.inputs['Roughness']
        if roughness_socket.is_linked:
            roughness_image = bpy.data.images.new(
                name=f"{obj.name}_Roughness",
                width=layer_settings.texture_resolution,
                height=layer_settings.texture_resolution,
                alpha=False,
                float_buffer=False
            )
            roughness_image.colorspace_settings.name = 'Non-Color'
            
            roughness_tex_node = nodes.new(type='ShaderNodeTexImage')
            roughness_tex_node.image = roughness_image
            roughness_tex_node.location = (principled.location.x - 400, principled.location.y - 600)
            
            # Select for baking
            for node in nodes:
                node.select = False
            roughness_tex_node.select = True
            nodes.active = roughness_tex_node
            
            # Get the node connected to roughness
            for link in links:
                if link.to_socket == roughness_socket:
                    # Disconnect material output and connect roughness source
                    for l in list(links):
                        if l.to_node == material_output:
                            links.remove(l)
                    links.new(link.from_socket, material_output.inputs['Surface'])
                    
                    # Bake
                    bpy.ops.object.bake(type='EMIT')
                    
                    # Restore connection
                    links.new(principled.outputs['BSDF'], material_output.inputs['Surface'])
                    
                    # Connect baked texture
                    links.new(roughness_tex_node.outputs['Color'], principled.inputs['Roughness'])
                    break
                
        # Bake Ambient Occlusion
        print("Baking ambient occlusion...")
        ao_image = bpy.data.images.new(
            name=f"{obj.name}_AO",
            width=layer_settings.texture_resolution,
            height=layer_settings.texture_resolution,
            alpha=False,
            float_buffer=False
        )
        ao_image.colorspace_settings.name = 'Non-Color'
        
        # Just call the bake function - it handles everything including samples
        bake_ambient_occlusion(obj, ao_image, context, layer_settings)
        
        print("Baked ambient occlusion")
        
        # Store the AO image reference for later glTF node creation
        obj["_temp_ao_image"] = ao_image  # Store as custom property
        
        # This ensures the node is properly set up for export
        if ao_image:
            create_gltf_output_node(mat, ao_image)
            print(f"Created glTF Material Output node for {mat.name}")
        
    except Exception as e:
        print(f"Baking failed: {str(e)}")
        return False
    
    finally:
        # Restore render settings
        context.scene.render.engine = original_engine
        context.scene.cycles.samples = original_samples
        context.scene.cycles.use_denoising = original_use_denoising
    
    return True

def bake_ambient_occlusion(obj, target_image, context, layer_setting):
    """Bake ambient occlusion for the object"""
    temp_nodes = []
    materials = [slot.material for slot in obj.material_slots if slot.material]
    
    for mat in materials:
        if not mat.use_nodes:
            continue
            
        nodes = mat.node_tree.nodes
        
        tex_node = nodes.new('ShaderNodeTexImage')
        tex_node.image = target_image
        tex_node.select = True
        temp_nodes.append((mat, tex_node))
        
        nodes.active = tex_node
    
    # Store and set AO-specific samples FROM UI
    original_samples = context.scene.cycles.samples
    context.scene.cycles.samples = layer_setting.ao_samples  # Use UI value
    
    # Set AO distance FROM UI
    if context.scene.world and hasattr(context.scene.world, 'light_settings'):
        context.scene.world.light_settings.distance = layer_setting.ao_distance  # Use UI value
    
    # Bake AO
    bpy.ops.object.bake(type='AO', use_clear=True, margin=layer_setting.bake_margin)
    
    # Restore original samples
    context.scene.cycles.samples = original_samples
    
    # Clean up temp nodes
    for mat, node in temp_nodes:
        mat.node_tree.nodes.remove(node)

def create_gltf_output_node(material, ao_image):
    """Create glTF Material Output node and connect AO"""
    if not material.use_nodes:
        return
        
    nodes = material.node_tree.nodes
    links = material.node_tree.links
    
    # Check if glTF node already exists
    gltf_node = None
    for node in nodes:
        if node.type == 'GROUP' and node.node_tree and node.node_tree.name == "glTF Material Output":
            gltf_node = node
            break
    
    # Create node group if it doesn't exist
    if not gltf_node:
        if "glTF Material Output" not in bpy.data.node_groups:
            node_group = bpy.data.node_groups.new(name="glTF Material Output", type='ShaderNodeTree')
            
            # Create input socket for Occlusion
            node_group.interface.new_socket(name="Occlusion", in_out='INPUT', socket_type='NodeSocketFloat')
            
            # Add input/output nodes
            group_input = node_group.nodes.new('NodeGroupInput')
            group_input.location = (0, 0)
            
            group_output = node_group.nodes.new('NodeGroupOutput')
            group_output.location = (200, 0)
        
        # Create instance of the node group
        gltf_node = nodes.new('ShaderNodeGroup')
        gltf_node.node_tree = bpy.data.node_groups["glTF Material Output"]
        gltf_node.location = (300, -300)
        gltf_node.name = "glTF Material Output"
    
    # Find or create AO texture node
    ao_tex_node = None
    for node in nodes:
        if node.type == 'TEX_IMAGE' and node.image == ao_image:
            ao_tex_node = node
            break
    
    if not ao_tex_node:
        ao_tex_node = nodes.new('ShaderNodeTexImage')
        ao_tex_node.image = ao_image
        ao_tex_node.location = (0, -300)
    
    # Connect AO to glTF node
    links.new(ao_tex_node.outputs['Color'], gltf_node.inputs['Occlusion'])


class LayerGeometrySettings(PropertyGroup):
    """Geometry settings for each layer"""
    
    layer_name: StringProperty(
        name="Layer Name",
        description="Name of the layer this settings belong to",
        default=""
    )
    
    show_layer: BoolProperty(
        name="Show Layer",
        description="Show/hide this layer's contents",
        default=False
    )
    
    show_expanded: BoolProperty(
        name="Show Expanded",
        description="Show/hide geomoetry settings for this layer",
        default=False
    )
    
    show_curve_expanded: BoolProperty(
        name="Show Curve Expanded",
        description="Show/hide curve resolution for this layer",
        default=False
    )
    
    layer_offset_percentage: FloatProperty(
        name="Layer Offset",
        description="Offset from layer below as percentage of this layer's height",
        default=0.0,
        min=-100.0,
        max=100.0,
        update=lambda self, context: self.update_layer_offset(context)
    )

    def get_offset_distance(self):
        """Calculate the actual offset distance in scene units"""
        if self.extrusion_depth == 0:
            return 0
        return self.extrusion_depth * (self.layer_offset_percentage / 100.0)

    def set_offset_distance(self, value):
        """Set offset percentage based on distance value"""
        if self.extrusion_depth == 0:
            self.layer_offset_percentage = 0
        else:
            # Calculate percentage from distance
            percentage = (value / self.extrusion_depth) * 100.0
            # Clamp to Â±100%
            new_percentage = max(-100, min(100, percentage))
            
            # Set the value without triggering the update callback
            old_percentage = self.layer_offset_percentage
            self.layer_offset_percentage = new_percentage
            
            # Manually update positions only if value changed
            if abs(new_percentage - old_percentage) > 0.001:
                self.update_layer_positions(bpy.context)

    layer_offset_distance_ui: FloatProperty(
        name="Layer Offset",
        description="Offset distance from layer below",
        get=get_offset_distance,
        set=set_offset_distance,
        unit='LENGTH',
        precision=3,
        step=0.001,  
        subtype='DISTANCE'
    )
    
    auto_adjust_layers: BoolProperty(
        name="Auto-Adjust Layers Above",
        description="Automatically adjust positions of layers above when changing extrusion or offset",
        default=True,
        update=lambda self, context: self.update_auto_adjust_layers(context)
    )

    layer_z_position: FloatProperty(
        name="Z Position",
        description="Absolute Z position of the layer center",
        default=0.0,
        unit='LENGTH',
        precision=3,
        update=lambda self, context: self.update_z_position(context)
    )
    
    def update_auto_adjust_layers(self, context):
        """Update auto adjust layers and sync if enabled"""
        global _sync_in_progress
        
        # Skip if we're already syncing
        if _sync_in_progress:
            self.update_layer_positions(context)
            return
        
        # When turning OFF auto-adjust, layers above need to store their current positions
        if not self.auto_adjust_layers:
            props = context.scene.svg_layers_props
            layer_number = int(self.layer_name.split()[-1]) if self.layer_name.split()[-1].isdigit() else 0
            
            # Find and update the next layer
            next_layer_name = f"Layer {layer_number + 1}"
            for setting in props.layer_settings:
                if setting.layer_name == next_layer_name:
                    obj = bpy.data.objects.get(next_layer_name)
                    if obj:
                        # Store its current position as Z position
                        setting.layer_z_position = obj.location.z
                    break
        
        # Update layer positions
        self.update_layer_positions(context)
        
        # Sync to other layers if sync is enabled
        props = context.scene.svg_layers_props
        if props.sync_auto_adjust_layers and not _sync_in_progress:
            _sync_in_progress = True
            for other_setting in props.layer_settings:
                if other_setting.layer_name != self.layer_name:
                    other_setting.auto_adjust_layers = self.auto_adjust_layers
            _sync_in_progress = False
        
        self.check_preset_change(context)
    
    def update_z_position(self, context):
        """Update layer position when Z position changes directly"""
        global _sync_in_progress
        
        obj = bpy.data.objects.get(self.layer_name)
        if obj:
            # Z position represents the BOTTOM of the layer
            # Calculate center from bottom + half extrusion
            if self.extrusion_depth > 0:
                obj.location.z = self.layer_z_position + (self.extrusion_depth / 2)
            else:
                obj.location.z = self.layer_z_position
            context.view_layer.update()
        
        # Sync to other layers if sync is enabled
        props = context.scene.svg_layers_props
        if props.sync_z_position and not _sync_in_progress:
            _sync_in_progress = True
            for other_setting in props.layer_settings:
                if (other_setting.layer_name != self.layer_name and 
                    other_setting.layer_name != "Layer 1"):
                    # Check if this other layer also needs manual positioning
                    other_layer_number = int(other_setting.layer_name.split()[-1]) if other_setting.layer_name.split()[-1].isdigit() else 0
                    if other_layer_number > 1:
                        prev_layer_name = f"Layer {other_layer_number - 1}"
                        prev_auto_adjust = True
                        for prev_setting in props.layer_settings:
                            if prev_setting.layer_name == prev_layer_name:
                                prev_auto_adjust = prev_setting.auto_adjust_layers
                                break
                        
                        # Only sync if this layer also uses Z Position
                        if not prev_auto_adjust:
                            other_setting.layer_z_position = self.layer_z_position
            _sync_in_progress = False
        
        self.check_preset_change(context)
    
    # Curve Resolution
    resolution_u: IntProperty(
        name="Curve Resolution U",
        description="Resolution for curve preview and render",
        default=16,
        min=1,
        max=64,
        update=lambda self, context: self.update_curve_settings(context)
    )
    
    bevel_resolution: IntProperty(
        name="Bevel Resolution",
        description="Resolution of the bevel curve",
        default=4,
        min=0,
        max=32,
        update=lambda self, context: self.update_curve_settings(context)
    )
    
    enable_points_redistribution: BoolProperty(
        name="Enable Points Redistribution",
        description="Enable automatic curve points redistribution",
        default=False,
        update=lambda self, context: self.handle_redistribution_toggle(context)
    )
    
    point_spacing: FloatProperty(
        name="Point Spacing",
        description="Distance between consecutive points",
        default=0.0005,
        min=0.0001,
        max=10.0,
        precision=4,
        unit='LENGTH'
    )
    
    straight_removal: IntProperty(
        name="Straight Edge Optimization",
        description="Percentage of points to remove from straight edges (0-100%)",
        default=100,
        min=0,
        max=100,
        subtype='PERCENTAGE'
    )
    
    straight_edge_tolerance: FloatProperty(
        name="Straight Edge Tolerance",
        description="Tolerance for detecting straight edges",
        default=0.0001,
        min=0.0,
        max=0.01,
        precision=6
    )
    
    # Store original curve data
    stored_curve_data: StringProperty(
        name="Stored Curve Data",
        description="JSON representation of original curve data",
        default=""
    )
    
    extrusion_depth: FloatProperty(
        name="Extrusion Depth",
        description="Target depth for extrusion (Z dimension) in meters",
        default=0.0,
        min=0.0,
        max=10.0,
        precision=3,
        unit='LENGTH',
        update=lambda self, context: self.update_shape(context)
    )
    
    is_updating: BoolProperty(
        name="Is Updating",
        description="Flag to prevent recursive updates",
        default=False
    )
    
    bevel_depth: FloatProperty(
        name="Bevel Depth",
        description="Bevel depth in meters (max = edges touching)",
        default=0.0,
        min=0.0,
        max=1.0,  # This will be dynamically overridden
        precision=6,
        step=0.0005,
        unit='LENGTH',
        update=lambda self, context: self.update_bevel_depth(context)
    )
    
    use_curve_offset: BoolProperty(
        name="Geometry Offset",
        description="Set curve geometry offset to negative bevel depth value",
        default=False,
        update=lambda self, context: self.update_offset_only(context)
    )
    
    geometry_rotation: FloatProperty(
        name="Geometry Rotation",
        description="Rotation angle for curve geometry in Z axis (degrees)",
        default=0.0,
        min=-360.0,
        max=360.0,
        precision=1,
        update=lambda self, context: self.update_geometry_rotation(context)
    )
    
    geometry_rotation_last: FloatProperty(
        name="Last Geometry Rotation",
        description="Previous rotation value for delta calculation",
        default=0.0,
        options={'HIDDEN'}  # Hidden from UI
    )
    
    fill_mode: EnumProperty(
        name="Fill Mode",
        description="Fill mode for the curve",
        items=[
            ('NONE', "None", "No fill"),
            ('BACK', "Back", "Fill back"),
            ('FRONT', "Front", "Fill front"),
            ('BOTH', "Both", "Fill both sides")
        ],
        default='BOTH',
        update=lambda self, context: self.update_fill_mode(context)
    )
    
    show_materials_expanded: BoolProperty(
        name="Show Materials Expanded",
        description="Show/hide materials settings for this layer",
        default=False
    )

    material_mode: EnumProperty(
        name="Material Mode",
        description="Choose material mode",
        items=[
            ('PRESERVED', "Preserved", "Use preserved material settings"),
            ('CUSTOM', "Custom", "Use custom material settings")
        ],
        default='PRESERVED',
        update=lambda self, context: self.update_material_mode(context)
    )

    use_ambient_occlusion: BoolProperty(
        name="Ambient Occlusion",
        description="Add Ambient Occlusion to material",
        default=True,
        update=lambda self, context: self.update_ambient_occlusion(context)
    )

    show_ao_settings: BoolProperty(
        name="Show AO Settings",
        description="Show/hide AO settings when enabled",
        default=False
    )

    ao_samples: IntProperty(
        name="Samples",  # Changed label here
        description="Number of samples for Ambient Occlusion",
        default=256,
        min=1,
        max=1024,
        update=lambda self, context: self.update_ao_settings(context)
    )

    ao_distance: FloatProperty(
        name="Distance",  # Changed label here
        description="Distance for Ambient Occlusion effect",
        default=0.05,
        min=0.0,
        max=100.0,
        precision=3,
        unit='LENGTH',
        update=lambda self, context: self.update_ao_settings(context)
    )
    
    material_metallic: FloatProperty(
        name="Metallic",
        description="Metallic value for the material",
        default=0.0,
        min=0.0,
        max=1.0,
        precision=3,
        update=lambda self, context: self.update_material_properties(context)
    )

    material_roughness: FloatProperty(
        name="Roughness", 
        description="Roughness value for the material",
        default=0.5,
        min=0.0,
        max=1.0,
        precision=3,
        update=lambda self, context: self.update_material_properties(context)
    )
    
    def get_base_color(self):
        """Get base color from the actual material"""
        obj = bpy.data.objects.get(self.layer_name)
        if obj and obj.data.materials and obj.data.materials[0]:
            mat = obj.data.materials[0]
            if mat.use_nodes:
                for node in mat.node_tree.nodes:
                    if node.type == 'BSDF_PRINCIPLED':
                        return node.inputs['Base Color'].default_value
        return (0.8, 0.8, 0.8, 1.0)  # Default gray if not found

    def set_base_color(self, value):
        """Set base color to the material"""
        obj = bpy.data.objects.get(self.layer_name)
        if obj and obj.data.materials and obj.data.materials[0]:
            mat = obj.data.materials[0]
            if mat.use_nodes:
                for node in mat.node_tree.nodes:
                    if node.type == 'BSDF_PRINCIPLED':
                        node.inputs['Base Color'].default_value = value
                        break

    material_base_color: FloatVectorProperty(
        name="Base Color",
        description="Base color from the imported material",
        subtype='COLOR',
        min=0.0,
        max=1.0,
        size=4,
        default=(0.8, 0.8, 0.8, 1.0),
        get=get_base_color,
        set=set_base_color
    )
    
    # UV Unwrap properties
    show_uv_settings: BoolProperty(
        name="UV Unwrap",
        description="Show UV unwrap settings",
        default=False
    )
    
    uv_method: EnumProperty(
        name="UV Method",
        description="UV unwrapping method",
        items=[
            ('MOF', "MOF UV Unwrap", "Use Ministry of Flat unwrapper"),
            ('SMART', "Smart UV Project", "Smart UV Project unwrapping"),
            ('CUBE', "Cube Projection", "Cube projection unwrapping"),
        ],
        default='MOF',
        update=lambda self, context: self.update_uv_settings(context)
    )
    
    # MOF Settings
    mof_separate_hard_edges: BoolProperty(
        name="Separate Hard Edges",
        default=True,
        description="Split edges that are both marked as seam and set as hard"
    )

    mof_separate_marked_edges: BoolProperty(
        name="Separate Marked Edges",
        default=True,
        description="Split the mesh along all marked edges"
    )

    mof_overlap_identical: BoolProperty(
        name="Overlap Identical Parts",
        default=False,
        description="Allow identical mesh parts to overlap in UV space"
    )

    mof_overlap_mirrored: BoolProperty(
        name="Overlap Mirrored Parts",
        default=False,
        description="Allow mirrored parts to overlap in UV space"
    )

    mof_world_scale: BoolProperty(
        name="World Scale UV",
        default=True,
        description="Apply the world scale to UV coordinates"
    )

    mof_use_normals: BoolProperty(
        name="Use Normals",
        default=False,
        description="Enable the use of vertex normals during UV calculation"
    )

    mof_suppress_validation: BoolProperty(
        name="Suppress Validation",
        default=False,
        description="Disable validation checks in the external tool"
    )

    mof_smooth: BoolProperty(
        name="Smooth",
        default=False,
        description="Disable for Hard Surface models if you see stretching"
    )

    mof_keep_original: BoolProperty(
        name="Keep Original Mesh",
        default=False,
        description="Duplicate the original mesh before processing"
    )

    mof_triangulate: BoolProperty(
        name="Triangulate",
        default=False,
        description="Triangulate the mesh before export"
    )
    
    uv_angle_limit: FloatProperty(
        name="Angle Limit",
        description="Maximum angle between faces to treat as continuous",
        default=66.0,
        min=0.0,
        max=90.0,
        precision=1,
        update=lambda self, context: self.update_uv_settings(context)
    )
    
    uv_margin_method: EnumProperty(
        name="Margin Method",
        description="Method to calculate margins between UV islands",
        items=[
            ('SCALED', "Scaled", "Scale margin by island size"),
            ('ADD', "Add", "Simple add margin"),
            ('FRACTION', "Fraction", "Margin as fraction of UV space")
        ],
        default='ADD',
        update=lambda self, context: self.update_uv_settings(context)
    )
    
    uv_rotate_method: EnumProperty(
        name="Rotation Method",
        description="Method to rotate UV islands",
        items=[
            ('AXIS_ALIGNED', "Axis-aligned (Vertical)", "Align to closest axis"),
            ('AXIS_ALIGNED_X', "Axis-aligned (Horizontal)", "Align to X axis"),
            ('AXIS_ALIGNED_Y', "Axis-aligned (Vertical)", "Align to Y axis")
        ],
        default='AXIS_ALIGNED',
        update=lambda self, context: self.update_uv_settings(context)
    )
    
    uv_island_margin: FloatProperty(
        name="Island Margin",
        description="Space between UV islands",
        default=0.005,
        min=0.0,
        max=1.0,
        precision=3,
        update=lambda self, context: self.update_uv_settings(context)
    )
    
    uv_area_weight: FloatProperty(
        name="Area Weight",
        description="Weight for balancing island sizes",
        default=0.0,
        min=0.0,
        max=1.0,
        precision=2,
        update=lambda self, context: self.update_uv_settings(context)
    )
    
    uv_correct_aspect: BoolProperty(
        name="Correct Aspect",
        description="Correct aspect ratio of UV islands",
        default=True,
        update=lambda self, context: self.update_uv_settings(context)
    )
    
    uv_scale_to_bounds: BoolProperty(
        name="Scale to Bounds",
        description="Scale UV islands to fill UV bounds",
        default=False,
        update=lambda self, context: self.update_uv_settings(context)
    )
    
    cube_size: FloatProperty(
        name="Cube Size",
        description="Size of the cube for projection (auto-calculated from object)",
        default=1.0,
        min=0.001,
        max=100.0,
        precision=3,
        update=lambda self, context: self.update_uv_settings(context)
    )

    cube_correct_aspect: BoolProperty(
        name="Correct Aspect",
        description="Correct aspect ratio for cube projection",
        default=True,
        update=lambda self, context: self.update_uv_settings(context)
    )

    cube_clip_to_bounds: BoolProperty(
        name="Clip to Bounds",
        description="Clip UV coordinates to bounds",
        default=False,
        update=lambda self, context: self.update_uv_settings(context)
    )

    cube_scale_to_bounds: BoolProperty(
        name="Scale to Bounds",
        description="Scale UV coordinates to bounds",
        default=False,
        update=lambda self, context: self.update_uv_settings(context)
    )
    
    # UV Packing properties
    enable_uv_packing: BoolProperty(
        name="Packing",
        description="Enable UV island packing after unwrapping",
        default=True,
        update=lambda self, context: self.update_uv_settings(context)
    )

    show_packing_settings: BoolProperty(
        name="Show Packing Settings",
        description="Show/hide packing settings",
        default=False
    )

    pack_shape_method: EnumProperty(
        name="Shape Method",
        description="Method for packing UV islands",
        items=[
            ('CONCAVE', "Exact Shape (Concave)", "Use exact shape including concave areas"),
            ('CONVEX', "Convex Hull", "Use convex hull of islands"),
            ('AABB', "Bounding Box", "Use axis-aligned bounding boxes")
        ],
        default='CONCAVE',
        update=lambda self, context: self.update_uv_settings(context)
    )

    pack_scale: BoolProperty(
        name="Scale",
        description="Scale islands to fit UV space",
        default=True,
        update=lambda self, context: self.update_uv_settings(context)
    )

    pack_rotate: BoolProperty(
        name="Rotate",
        description="Rotate islands for better packing",
        default=True,
        update=lambda self, context: self.update_uv_settings(context)
    )

#    pack_rotation_method: EnumProperty(
#        name="Rotation Method",
#        description="Rotation method for packing",
#        items=[
#            ('ANY', "Any", "Any angle"),
#            ('AXIS_ALIGNED', "Axis-aligned", "Only 90 degree rotations"),
#            ('CARDINAL', "Cardinal", "0, 90, 180, 270 degrees")
#        ],
#        default='ANY',
#        update=lambda self, context: self.update_uv_settings(context)
#    )

    pack_margin_method: EnumProperty(
        name="Margin Method",
        description="Method to calculate margins",
        items=[
            ('SCALED', "Scaled", "Scale margin by island size"),
            ('ADD', "Add", "Fixed margin"),
            ('FRACTION', "Fraction", "Margin as fraction of UV space")
        ],
        default='ADD',
        update=lambda self, context: self.update_uv_settings(context)
    )

    pack_margin: FloatProperty(
        name="Margin",
        description="Space between packed islands",
        default=0.005,
        min=0.0,
        max=0.5,
        precision=3,
        update=lambda self, context: self.update_uv_settings(context)
    )

    pack_pin_islands: BoolProperty(
        name="Lock Pinned Islands",
        description="Don't move pinned islands",
        default=False,
        update=lambda self, context: self.update_uv_settings(context)
    )

    pack_pin_method: EnumProperty(
        name="Lock Method",
        description="How to handle pinned islands",
        items=[
            ('LOCKED', "Locked", "Fully lock pinned islands"),
            ('SCALE', "Lock Scale", "Lock scale only"),
            ('ROTATION', "Lock Rotation", "Lock rotation only"),
            ('ROTATION_SCALE', "Lock Scale & Rotation", "Lock both scale and rotation")
        ],
        default='LOCKED',
        update=lambda self, context: self.update_uv_settings(context)
    )

    pack_merge_overlapping: BoolProperty(
        name="Merge Overlapping",
        description="Merge overlapping islands before packing",
        default=False,
        update=lambda self, context: self.update_uv_settings(context)
    )

    pack_udim_source: EnumProperty(
        name="Pack to",
        description="Target for packing",
        items=[
            ('CLOSEST_UDIM', "Closest UDIM", "Pack to closest UDIM tile"),
            ('ACTIVE_UDIM', "Active UDIM", "Pack to active UDIM tile"),
            ('ORIGINAL', "Original", "Keep in original tiles")
        ],
        default='CLOSEST_UDIM',
        update=lambda self, context: self.update_uv_settings(context)
    )
    
    # Baking properties
    bake_method: EnumProperty(
        name="Bake Method",
        description="Method for baking textures",
        items=[
            ('EMIT', "Color Bake as Emit", "Bake color as emission"),
            ('DIFFUSE', "Color Bake as Diffuse (Baking Lights)", "Bake with lighting")
        ],
        default='EMIT',
        update=lambda self, context: self.update_bake_settings(context)
    )
    
    show_baking_settings: BoolProperty(
        name="Baking",
        description="Show baking settings",
        default=False
    )
    
    bake_samples: IntProperty(
        name="Samples",
        description="Number of samples for baking",
        default=100,
        min=1,
        max=4096,
        update=lambda self, context: self.update_bake_settings(context)
    )
    
    bake_margin: IntProperty(
        name="Margin",
        description="Baking margin in pixels",
        default=32,
        min=0,
        max=64,
        update=lambda self, context: self.update_bake_settings(context)
    )
    
    texture_resolution: IntProperty(
        name="Texture Resolution",
        description="Resolution for baked textures",
        default=2048,
        min=256,
        max=8192,
        step=256,
        update=lambda self, context: self.update_bake_settings(context)
    )
    
    def update_bevel_depth(self, context):
        """Update bevel - clamped to max where edges touch"""
        global _sync_in_progress
        
        # Prevent recursive updates
        if self.is_updating or _sync_in_progress:
            return
            
        obj = bpy.data.objects.get(self.layer_name)
        if not obj or obj.type != 'CURVE':
            return
        
        # Set flag to prevent recursion
        self.is_updating = True
        
        # Calculate maximum allowed bevel (edges touching)
        max_allowed_bevel = self.extrusion_depth * 0.5
#        max_allowed_bevel = self.extrusion_depth * 0.0554
        
        # Clamp the value to maximum
        if self.bevel_depth > max_allowed_bevel:
            self.bevel_depth = max_allowed_bevel
        
        # Apply the bevel
        obj.data.bevel_depth = self.bevel_depth
        
        # Apply offset if enabled
        if self.use_curve_offset and obj.data.bevel_depth > 0:
            obj.data.offset = -obj.data.bevel_depth
        else:
            obj.data.offset = 0
        
        # Add/remove Weld modifier based on bevel being at maximum
        if max_allowed_bevel > 0:
            is_at_maximum = abs(self.bevel_depth - max_allowed_bevel) < 0.000001
            
            # Check if Weld modifier exists
            weld_modifier = None
            for modifier in obj.modifiers:
                if modifier.type == 'WELD':
                    weld_modifier = modifier
                    break
            
            if is_at_maximum and not weld_modifier:
                # Add Weld modifier when at 100%
                weld_mod = obj.modifiers.new(name="Weld", type='WELD')
                weld_mod.merge_threshold = 0.000002
                
                # Find Edge Split modifier and move Weld above it
                edge_split_index = -1
                for i, modifier in enumerate(obj.modifiers):
                    if modifier.type == 'EDGE_SPLIT':
                        edge_split_index = i
                        break
                
                # If Edge Split exists, move Weld above it
                if edge_split_index != -1:
                    # Move Weld up until it's above Edge Split
                    while obj.modifiers.find(weld_mod.name) > edge_split_index:
                        bpy.ops.object.modifier_move_up(modifier=weld_mod.name)
            
            elif not is_at_maximum and weld_modifier:
                # Remove Weld modifier when below 100%
                obj.modifiers.remove(weld_modifier)
        
        # Clear flag BEFORE calling update_shape
        self.is_updating = False
        
        # Sync to other layers if sync is enabled
        props = context.scene.svg_layers_props
        if props.sync_bevel_depth and not _sync_in_progress:
            _sync_in_progress = True
            for other_setting in props.layer_settings:
                if other_setting.layer_name != self.layer_name:
                    # Get the other layer's object
                    other_obj = bpy.data.objects.get(other_setting.layer_name)
                    if other_obj and other_obj.type == 'CURVE':
                        # Calculate max for the other layer
                        other_max = other_setting.extrusion_depth * 0.5
                        
                        # Set the value (clamped to that layer's max)
                        actual_value = min(self.bevel_depth, other_max)
                        other_setting.bevel_depth = actual_value
                        
                        # Apply directly to the curve object
                        other_obj.data.bevel_depth = actual_value
                        
                        # Handle offset if enabled
                        if other_setting.use_curve_offset and actual_value > 0:
                            other_obj.data.offset = -actual_value
                        else:
                            other_obj.data.offset = 0
                        
                        # IMPORTANT: Call update_shape to compensate extrusion
                        other_setting.update_shape(context)
                        
                        # Handle Weld modifier at maximum
#                        if other_max > 0:
#                            is_at_maximum = abs(actual_value - other_max) < 0.000001
#                            
#                            weld_modifier = None
#                            for modifier in other_obj.modifiers:
#                                if modifier.type == 'WELD':
#                                    weld_modifier = modifier
#                                    break
#                            
#                            if is_at_maximum and not weld_modifier:
#                                weld_mod = other_obj.modifiers.new(name="Weld", type='WELD')
#                                weld_mod.merge_threshold = 0.000002
#                            elif not is_at_maximum and weld_modifier:
#                                other_obj.modifiers.remove(weld_modifier)
            
            _sync_in_progress = False
        
        # Update the shape to compensate extrusion
        self.update_shape(context)
        self.check_preset_change(context)
    
    def update_offset_only(self, context):
        """Update only the offset without recalculating other values"""
        global _sync_in_progress
        
        # Skip if we're already syncing
        if _sync_in_progress:
            return
            
        # Find the curve object with this layer name
        obj = bpy.data.objects.get(self.layer_name)
        
        if obj and obj.type == 'CURVE':
            # Only update the offset value
            if self.use_curve_offset:
                obj.data.offset = -obj.data.bevel_depth
            else:
                obj.data.offset = 0
            
            # Update view
            context.view_layer.update()
        
        # Add sync logic
        props = context.scene.svg_layers_props
        if props.sync_geometry_offset and not _sync_in_progress:
            _sync_in_progress = True
            for other_setting in props.layer_settings:
                if other_setting.layer_name != self.layer_name:
                    other_setting.use_curve_offset = self.use_curve_offset
            _sync_in_progress = False
        
        # Check preset change after ALL operations
        self.check_preset_change(context)
    
    def update_geometry_rotation(self, context):
        """Idempotent geometry rotation with object-backed state."""
        global _sync_in_progress

        obj = bpy.data.objects.get(self.layer_name)
        if not obj or obj.type != 'CURVE':
            self.check_preset_change(context)
            return

        # Source of truth: what rotation (in degrees) has already been applied to this object?
        # Stored as a custom ID property so it survives preset reloads and add-on restarts.
        APPLIED_KEY = "svg_layers_applied_rotation_deg"
        prev_deg = float(obj.get(APPLIED_KEY, 0.0))
        target_deg = float(self.geometry_rotation)
        delta_deg = target_deg - prev_deg

        # If a preset is being loaded, avoid accidental double-application:
        # - If the object already carries an applied rotation marker, do a normal idempotent update (delta vs prev).
        # - If it has no marker and appears unrotated (Z euler ~ 0), apply full target.
        # - If it has no marker and Z euler is non-zero (likely previously rotated by this tool),
        #   treat it as already applied and just sync the marker.
        props = context.scene.svg_layers_props
        if getattr(props, "loading_preset", False):
            if APPLIED_KEY not in obj.keys():
                if abs(obj.rotation_euler[2]) < 1e-8:
                    # Fresh model: apply full rotation once.
                    delta_deg = target_deg
                else:
                    # Already rotated previously: don't touch geometry, just record it.
                    obj[APPLIED_KEY] = target_deg
                    self.geometry_rotation_last = target_deg
                    self.check_preset_change(context)
                    return

        if abs(delta_deg) < 1e-6:
            # Nothing to do; keep bookkeeping in sync and exit.
            self.geometry_rotation_last = target_deg
            obj[APPLIED_KEY] = target_deg
            self.check_preset_change(context)
            return

        delta_radians = math.radians(delta_deg)
        cos_r = math.cos(delta_radians)
        sin_r = math.sin(delta_radians)

        curve_data = obj.data
        for spline in curve_data.splines:
            if spline.type == 'BEZIER':
                for point in spline.bezier_points:
                    # control point
                    x, y = point.co.x, point.co.y
                    point.co.x = x * cos_r - y * sin_r
                    point.co.y = x * sin_r + y * cos_r
                    # handles
                    x, y = point.handle_left.x, point.handle_left.y
                    point.handle_left.x = x * cos_r - y * sin_r
                    point.handle_left.y = x * sin_r + y * cos_r
                    x, y = point.handle_right.x, point.handle_right.y
                    point.handle_right.x = x * cos_r - y * sin_r
                    point.handle_right.y = x * sin_r + y * cos_r
            else:  # POLY or NURBS
                for point in spline.points:
                    x, y = point.co.x, point.co.y
                    point.co.x = x * cos_r - y * sin_r
                    point.co.y = x * sin_r + y * cos_r

        curve_data.update_tag()

        # Compensate object Z rotation so the visual orientation stays the same.
        obj.rotation_euler[2] -= delta_radians

        # Bookkeeping
        self.geometry_rotation_last = target_deg
        obj[APPLIED_KEY] = target_deg

        # Optional sync across layers (unchanged from your code)
        props = context.scene.svg_layers_props
        if props.sync_geometry_rotation and not _sync_in_progress:
            _sync_in_progress = True
            for other_setting in props.layer_settings:
                if other_setting.layer_name != self.layer_name:
                    other_setting.geometry_rotation = self.geometry_rotation
            _sync_in_progress = False

        context.view_layer.update()
        self.check_preset_change(context)
    
    def update_curve_settings(self, context):
        """Update curve resolution settings"""
        global _sync_in_progress
        
        # Skip if we're already syncing
        if _sync_in_progress:
            return
            
        obj = bpy.data.objects.get(self.layer_name)
        if obj and obj.type == 'CURVE':
            # Update resolution settings
            obj.data.resolution_u = self.resolution_u
            obj.data.render_resolution_u = self.resolution_u
            obj.data.bevel_resolution = self.bevel_resolution
            
            # Update all splines
            for spline in obj.data.splines:
                spline.resolution_u = self.resolution_u
            
            context.view_layer.update()
            
            # Add sync logic
            props = context.scene.svg_layers_props
            
            # Sync resolution_u if enabled
            if props.sync_resolution_u and not _sync_in_progress:
                _sync_in_progress = True
                for other_setting in props.layer_settings:
                    if other_setting.layer_name != self.layer_name:
                        other_setting.resolution_u = self.resolution_u
                _sync_in_progress = False
                
            # Sync bevel_resolution if enabled
            if props.sync_bevel_resolution and not _sync_in_progress:
                _sync_in_progress = True
                for other_setting in props.layer_settings:
                    if other_setting.layer_name != self.layer_name:
                        other_setting.bevel_resolution = self.bevel_resolution
                _sync_in_progress = False
        
        # Check preset change after ALL operations
        self.check_preset_change(context)
    
    def handle_redistribution_toggle(self, context):
        """Handle enable/disable of points redistribution"""
        obj = bpy.data.objects.get(self.layer_name)
        if not obj or obj.type != 'CURVE':
            return
        
        if not self.enable_points_redistribution:
            # Restore original curve if we have stored data
            if self.stored_curve_data:
                import json
                try:
                    stored_data = json.loads(self.stored_curve_data)
                    # Convert stored data back to proper format
                    restored_data = {
                        'splines': []
                    }
                    for spline_data in stored_data['splines']:
                        spline = {
                            'type': spline_data['type'],
                            'use_cyclic_u': spline_data['use_cyclic_u'],
                            'points': []
                        }
                        for point_data in spline_data['points']:
                            if 'handle_left' in point_data:
                                spline['points'].append({
                                    'co': Vector(point_data['co']),
                                    'handle_left': Vector(point_data['handle_left']),
                                    'handle_right': Vector(point_data['handle_right']),
                                    'handle_left_type': point_data['handle_left_type'],
                                    'handle_right_type': point_data['handle_right_type']
                                })
                            else:
                                spline['points'].append({
                                    'co': Vector(point_data['co'])
                                })
                        restored_data['splines'].append(spline)
                    
                    # Restore the UNROTATED curve data
                    restore_curve_data(obj, restored_data)
                    self.stored_curve_data = ""
                    
                    # Now apply the current geometry rotation to the restored curve
                    if self.geometry_rotation != 0:
                        radians = math.radians(self.geometry_rotation)
                        cos_r = math.cos(radians)
                        sin_r = math.sin(radians)
                        
                        for spline in obj.data.splines:
                            if spline.type == 'BEZIER':
                                for point in spline.bezier_points:
                                    x, y = point.co.x, point.co.y
                                    point.co.x = x * cos_r - y * sin_r
                                    point.co.y = x * sin_r + y * cos_r
                                    
                                    x, y = point.handle_left.x, point.handle_left.y
                                    point.handle_left.x = x * cos_r - y * sin_r
                                    point.handle_left.y = x * sin_r + y * cos_r
                                    
                                    x, y = point.handle_right.x, point.handle_right.y
                                    point.handle_right.x = x * cos_r - y * sin_r
                                    point.handle_right.y = x * sin_r + y * cos_r
                            elif spline.type in ['POLY', 'NURBS']:
                                for point in spline.points:
                                    x, y = point.co.x, point.co.y
                                    point.co.x = x * cos_r - y * sin_r
                                    point.co.y = x * sin_r + y * cos_r
                    
                        obj.data.update_tag()
                    
                    context.view_layer.update()
                    
                    # NEW: record applied rotation so future preset loads stay idempotent
                    obj["svg_layers_applied_rotation_deg"] = float(self.geometry_rotation)
                    
                except:
                    pass
        self.check_preset_change(context)
    
    def update_layer_offset(self, context):
        """Update layer positions when offset changes"""
        global _sync_in_progress
        
        # Skip if we're already syncing
        if _sync_in_progress:
            return
        
        self.update_layer_positions(context)
        
        # Sync to other layers if sync is enabled
        props = context.scene.svg_layers_props
        if props.sync_layer_offset and not _sync_in_progress:
            _sync_in_progress = True
            
            # Get the actual distance value
            actual_distance = self.extrusion_depth * (self.layer_offset_percentage / 100.0)
            
            for other_setting in props.layer_settings:
                if other_setting.layer_name != self.layer_name and other_setting.layer_name != "Layer 1":
                    # Calculate what percentage would give this distance for the other layer
                    if other_setting.extrusion_depth > 0:
                        other_setting.layer_offset_percentage = (actual_distance / other_setting.extrusion_depth) * 100.0
                    else:
                        other_setting.layer_offset_percentage = 0
            _sync_in_progress = False
        
        self.check_preset_change(context)    
    
    def update_layer_positions(self, context):
        """Update Z positions of all layers based on their extrusions and offsets"""
        props = context.scene.svg_layers_props
        
        # Sort layers by their number
        sorted_layers = sorted(props.layer_settings, 
                             key=lambda x: int(x.layer_name.split()[-1]) if x.layer_name.split()[-1].isdigit() else 0)
        
        # Track the actual top position of each layer (including its offset)
        layer_top_positions = []
        
        # Find which layer triggered this update (if any)
        triggering_layer_index = -1
        if hasattr(self, 'layer_name'):
            for i, layer_setting in enumerate(sorted_layers):
                if layer_setting.layer_name == self.layer_name:
                    triggering_layer_index = i
                    break
        
        for i, layer_setting in enumerate(sorted_layers):
            obj = bpy.data.objects.get(layer_setting.layer_name)
            if not obj:
                continue
            
            if i == 0:
                # Layer 1 - bottom stays at Z = 0, extrudes upward only
                if layer_setting.extrusion_depth > 0:
                    obj.location.z = layer_setting.extrusion_depth / 2
                    layer_top = layer_setting.extrusion_depth
                else:
                    obj.location.z = 0
                    layer_top = 0
                layer_top_positions.append(layer_top)
            else:
                # Check if this layer can auto-stack (depends on previous layer's auto_adjust)
                can_auto_stack = sorted_layers[i-1].auto_adjust_layers if i > 0 else True
                
                # Check if we should actually adjust this layer
                should_adjust = can_auto_stack
                
                # If a specific layer triggered this and has auto_adjust OFF, 
                # don't adjust layers above it
                if triggering_layer_index >= 0 and triggering_layer_index < i:
                    if not sorted_layers[triggering_layer_index].auto_adjust_layers:
                        should_adjust = False
                
                if should_adjust:
                    # Normal stacking behavior
                    z_pos = layer_top_positions[i-1]
                    
                    # Add half of current layer's extrusion to position its center
                    if layer_setting.extrusion_depth > 0:
                        z_pos += layer_setting.extrusion_depth / 2
                    
                    # Apply the offset based on percentage of this layer's height
                    if layer_setting.layer_offset_percentage != 0 and layer_setting.extrusion_depth > 0:
                        offset_amount = layer_setting.extrusion_depth * (layer_setting.layer_offset_percentage / 100.0)
                        z_pos += offset_amount
                    
                    # Set the position
                    obj.location.z = z_pos
                    # Update stored Z position (store the BOTTOM position, not center)
                    layer_setting.layer_z_position = z_pos - (layer_setting.extrusion_depth / 2 if layer_setting.extrusion_depth > 0 else 0)
                else:
                    # Manual positioning mode
                    # layer_z_position represents the BOTTOM of the layer
                    # Calculate center position from bottom position
                    if layer_setting.extrusion_depth > 0:
                        z_pos = layer_setting.layer_z_position + (layer_setting.extrusion_depth / 2)
                    else:
                        z_pos = layer_setting.layer_z_position
                    
                    obj.location.z = z_pos
                
                # Calculate this layer's actual top position
                if layer_setting.extrusion_depth > 0:
                    layer_top = z_pos + layer_setting.extrusion_depth / 2
                else:
                    layer_top = z_pos
                
                layer_top_positions.append(layer_top)
        
        # Update view
        context.view_layer.update()
    
    def update_ambient_occlusion(self, context, target_obj=None):
        """Add or remove Ambient Occlusion node from material"""
        global _sync_in_progress
        
        if _sync_in_progress:
            return
        
        # Sync to other layers if enabled
        props = context.scene.svg_layers_props
        if props.sync_ambient_occlusion and not _sync_in_progress:
            _sync_in_progress = True
            for other_setting in props.layer_settings:
                if other_setting.layer_name != self.layer_name:
                    other_setting.use_ambient_occlusion = self.use_ambient_occlusion
            _sync_in_progress = False
        
        self.check_preset_change(context)

    def update_ao_settings(self, context):
        """Update AO node settings"""
        global _sync_in_progress
        
        if _sync_in_progress:
            return
        
        # Just sync to other layers if enabled
        props = context.scene.svg_layers_props
        if props.sync_ambient_occlusion and not _sync_in_progress:
            _sync_in_progress = True
            for other_setting in props.layer_settings:
                if other_setting.layer_name != self.layer_name:
                    other_setting.ao_samples = self.ao_samples
                    other_setting.ao_distance = self.ao_distance
            _sync_in_progress = False
        
        self.check_preset_change(context)
    
    def update_material_properties(self, context, target_obj=None):
        """Update metallic and roughness in Principled BSDF"""
        global _sync_in_progress
        
        if _sync_in_progress:
            return
        
        obj = target_obj or bpy.data.objects.get(self.layer_name)
        if not obj or not obj.data.materials:
            return
        
        mat = obj.data.materials[0]
        if not mat.use_nodes:
            return
        
        nodes = mat.node_tree.nodes
        
        # Find Principled BSDF
        for node in nodes:
            if node.type == 'BSDF_PRINCIPLED':
                node.inputs['Metallic'].default_value = self.material_metallic
                node.inputs['Roughness'].default_value = self.material_roughness
                break
        
        # Sync to other layers if sync is enabled
        props = context.scene.svg_layers_props
        if props.sync_principled_bsdf and not _sync_in_progress:
            _sync_in_progress = True
            for other_setting in props.layer_settings:
                if other_setting.layer_name != self.layer_name:
                    other_setting.material_metallic = self.material_metallic
                    other_setting.material_roughness = self.material_roughness
            _sync_in_progress = False
        
        self.check_preset_change(context)
    
    def get_offset_distance(self):
        """Calculate the actual offset distance in scene units"""
        if self.extrusion_depth == 0:
            return 0
        return self.extrusion_depth * (self.layer_offset_percentage / 100.0)
    
    def update_shape(self, context):
        """Update Geometry settings for the layer's curve object"""
        # Prevent recursive updates
        if self.is_updating:
            return
            
        obj = bpy.data.objects.get(self.layer_name)
        
        if not obj or obj.type != 'CURVE':
            return
        
        # Set flag to prevent recursion
        self.is_updating = True
        
        # Check and adjust bevel if it exceeds new limit
        if self.extrusion_depth == 0:
            # No extrusion = no bevel
            self.bevel_depth = 0
            obj.data.bevel_depth = 0
        else:
            # Calculate new maximum allowed bevel
            max_allowed_bevel = self.extrusion_depth * 0.5
#            max_allowed_bevel = self.extrusion_depth * 0.0554
            
            # If current bevel exceeds new maximum, clamp it
            if self.bevel_depth > max_allowed_bevel:
                self.bevel_depth = max_allowed_bevel
                obj.data.bevel_depth = max_allowed_bevel
        
        # Clear flag
        self.is_updating = False
        
        # Apply bevel resolution
        obj.data.bevel_mode = 'ROUND'
        obj.data.bevel_resolution = self.bevel_resolution
        
        # First, temporarily remove bevel to calculate extrusion correctly
        original_bevel = obj.data.bevel_depth
        obj.data.bevel_depth = 0
        obj.data.offset = 0
        context.view_layer.update()
        
        # Handle extrusion WITHOUT bevel first
        if self.extrusion_depth == 0:
            obj.data.extrude = 0.0
            context.view_layer.update()
        else:
            # Binary search for correct extrusion (without bevel affecting it)
            target_z = self.extrusion_depth
            tolerance = 0.000001
            min_extrude = 0
            max_extrude = 5.0
            
            for i in range(50):
                test_extrude = (min_extrude + max_extrude) / 2
                obj.data.extrude = test_extrude
                context.view_layer.update()
                
                current_z = obj.dimensions.z
                
                if abs(current_z - target_z) < tolerance:
                    break
                elif current_z < target_z:
                    min_extrude = test_extrude
                else:
                    max_extrude = test_extrude
            
            # Store the correct extrusion value
            correct_extrusion = obj.data.extrude
        
        # Now apply the bevel
        obj.data.bevel_depth = original_bevel
        context.view_layer.update()
        
        # Check if bevel changed the Z dimension
        if self.extrusion_depth > 0 and original_bevel > 0:
            current_z = obj.dimensions.z
            
            # If Z grew beyond target, adjust extrusion to compensate
            if current_z > self.extrusion_depth + 0.000001:
                # Binary search to find extrusion that gives correct Z WITH bevel
                min_extrude = 0
                max_extrude = correct_extrusion
                
                for i in range(50):
                    test_extrude = (min_extrude + max_extrude) / 2
                    obj.data.extrude = test_extrude
                    context.view_layer.update()
                    
                    current_z = obj.dimensions.z
                    
                    if abs(current_z - self.extrusion_depth) < 0.000001:
                        break
                    elif current_z < self.extrusion_depth:
                        min_extrude = test_extrude
                    else:
                        max_extrude = test_extrude
        
        # Apply offset if enabled
        if self.use_curve_offset and obj.data.bevel_depth > 0:
            obj.data.offset = -obj.data.bevel_depth
        else:
            obj.data.offset = 0
        
        # Update all layer positions after changing shape
        self.update_layer_positions(context)
        
        # Final update
        context.view_layer.update()
        self.check_preset_change(context)
    
    def update_bake_settings(self, context):
        """Update bake settings (placeholder for now)"""
        pass
        

    def update_uv_settings(self, context):
        """Update UV settings and sync if enabled"""
        global _sync_in_progress
        
        # Skip if we're already syncing
        if _sync_in_progress:
            return
        
        # Sync to other layers if sync is enabled
        props = context.scene.svg_layers_props
        if props.sync_uv_unwrap and not _sync_in_progress:
            _sync_in_progress = True
            for other_setting in props.layer_settings:
                if other_setting.layer_name != self.layer_name:
                    # Sync all UV settings
                    other_setting.uv_method = self.uv_method
                    other_setting.uv_angle_limit = self.uv_angle_limit
                    other_setting.uv_margin_method = self.uv_margin_method
                    other_setting.uv_rotate_method = self.uv_rotate_method
                    other_setting.uv_island_margin = self.uv_island_margin
                    other_setting.uv_area_weight = self.uv_area_weight
                    other_setting.uv_correct_aspect = self.uv_correct_aspect
                    other_setting.uv_scale_to_bounds = self.uv_scale_to_bounds
                    # Cube settings
                    other_setting.cube_size = self.cube_size
                    other_setting.cube_correct_aspect = self.cube_correct_aspect
                    other_setting.cube_clip_to_bounds = self.cube_clip_to_bounds
                    other_setting.cube_scale_to_bounds = self.cube_scale_to_bounds
                    # MOF settings
                    other_setting.mof_separate_hard_edges = self.mof_separate_hard_edges
                    other_setting.mof_separate_marked_edges = self.mof_separate_marked_edges
                    other_setting.mof_overlap_identical = self.mof_overlap_identical
                    other_setting.mof_overlap_mirrored = self.mof_overlap_mirrored
                    other_setting.mof_world_scale = self.mof_world_scale
                    other_setting.mof_use_normals = self.mof_use_normals
                    other_setting.mof_suppress_validation = self.mof_suppress_validation
                    other_setting.mof_smooth = self.mof_smooth
                    other_setting.mof_keep_original = self.mof_keep_original
                    other_setting.mof_triangulate = self.mof_triangulate
                    # Packing settings
                    other_setting.enable_uv_packing = self.enable_uv_packing
                    other_setting.pack_shape_method = self.pack_shape_method
                    other_setting.pack_scale = self.pack_scale
                    other_setting.pack_rotate = self.pack_rotate
#                    other_setting.pack_rotation_method = self.pack_rotation_method
                    other_setting.pack_margin_method = self.pack_margin_method
                    other_setting.pack_margin = self.pack_margin
                    other_setting.pack_pin_islands = self.pack_pin_islands
                    other_setting.pack_pin_method = self.pack_pin_method
                    other_setting.pack_merge_overlapping = self.pack_merge_overlapping
                    other_setting.pack_udim_source = self.pack_udim_source
            _sync_in_progress = False
    
        self.check_preset_change(context)
            
    def update_bake_settings(self, context):
        """Update bake settings and sync if enabled"""
        global _sync_in_progress
        
        # Skip if we're already syncing
        if _sync_in_progress:
            return
        
        # Sync to other layers if sync is enabled
        props = context.scene.svg_layers_props
        if props.sync_baking and not _sync_in_progress:
            _sync_in_progress = True
            for other_setting in props.layer_settings:
                if other_setting.layer_name != self.layer_name:
                    # Sync all baking settings
                    other_setting.bake_method = self.bake_method
                    other_setting.bake_samples = self.bake_samples
                    other_setting.bake_margin = self.bake_margin
                    other_setting.texture_resolution = self.texture_resolution
            _sync_in_progress = False
            
        self.check_preset_change(context)

    def update_material_mode(self, context):
        """Update material mode and sync if enabled"""
        global _sync_in_progress
        
        # Skip if we're already syncing
        if _sync_in_progress:
            return
        
        # Sync to other layers if sync is enabled
        props = context.scene.svg_layers_props
        if hasattr(props, 'sync_material_mode') and props.sync_material_mode and not _sync_in_progress:
            _sync_in_progress = True
            for other_setting in props.layer_settings:
                if other_setting.layer_name != self.layer_name:
                    other_setting.material_mode = self.material_mode
            _sync_in_progress = False
            
        self.check_preset_change(context)

    def update_fill_mode(self, context):
        """Update curve fill mode"""
        global _sync_in_progress
        
        obj = bpy.data.objects.get(self.layer_name)
        if obj and obj.type == 'CURVE':
            print(f"[FILL DEBUG] Setting fill_mode={self.fill_mode} for {self.layer_name}")
            
            try:
                # DON'T TRANSLATE - use directly!
                obj.data.fill_mode = self.fill_mode
                context.view_layer.update()
                print(f"[FILL DEBUG] Successfully set fill_mode to {self.fill_mode}")
            except Exception as e:
                print(f"[FILL ERROR] Failed to set fill mode '{self.fill_mode}': {e}")
                print(f"[FILL ERROR] Available modes: {list(obj.data.bl_rna.properties['fill_mode'].enum_items.keys())}")
            
            # Sync to other layers if enabled
            props = context.scene.svg_layers_props
            if props.sync_fill_mode and not _sync_in_progress:
                _sync_in_progress = True
                for other_setting in props.layer_settings:
                    if other_setting.layer_name != self.layer_name:
                        other_setting.fill_mode = self.fill_mode
                        # Apply directly to the curve object
                        other_obj = bpy.data.objects.get(other_setting.layer_name)
                        if other_obj and other_obj.type == 'CURVE':
                            try:
                                other_obj.data.fill_mode = self.fill_mode
                            except:
                                pass
                _sync_in_progress = False
        
        self.check_preset_change(context)

    def check_preset_change(self, context):
        props = context.scene.svg_layers_props
        # Do NOT clear while a preset is being loaded
        if getattr(props, "loading_preset", False):
            return
        # Clear selection after any user-driven change
        if props.presets != 'NONE':
            props.presets = 'NONE'
            props.current_preset = ""


class SVGLayersProperties(PropertyGroup):
    """Main property group for the addon"""
    
    # Testing Model folder path
    testing_folder_path: StringProperty(
        name="Folder Location",
        description="Directory containing SVG files for testing",
        default="",
        maxlen=1024,
        subtype='DIR_PATH'
    )
    
    show_layers_expanded: BoolProperty(
        name="Show Layers Expanded",
        description="Show/hide all layers",
        default=True
    )
    
    # Collection of layer settings
    layer_settings: CollectionProperty(type=LayerGeometrySettings)
    
    # Sync properties for materials
    sync_ambient_occlusion: BoolProperty(
        name="Sync Ambient Occlusion",
        description="Synchronize Ambient Occlusion settings across all layers",
        default=True
    )
    
    sync_principled_bsdf: BoolProperty(
        name="Sync Principled BSDF",
        description="Synchronize Principled BSDF settings across all layers",
        default=True
    )
    
    sync_uv_unwrap: BoolProperty(
        name="Sync UV Unwrap",
        description="Synchronize UV Unwrap settings across all layers",
        default=True
    )
    
    sync_baking: BoolProperty(
        name="Sync Baking",
        description="Synchronize Baking settings across all layers",
        default=True
    )
    
    sync_material_mode: BoolProperty(
        name="Sync Material Mode",
        description="Synchronize Material Mode selection across all layers",
        default=True
    )
    
    sync_layer_offset: BoolProperty(
        name="Sync Layer Offset",
        description="Synchronize Layer Offset settings across all layers",
        default=False
    )

    sync_bevel_depth: BoolProperty(
        name="Sync Bevel Depth",
        description="Synchronize Bevel Depth settings across all layers",
        default=False
    )
    
    sync_geometry_offset: BoolProperty(
        name="Sync Geometry Offset",
        description="Synchronize Geometry Offset (checkbox) across all layers",
        default=False
    )

    sync_geometry_rotation: BoolProperty(
        name="Sync Geometry Rotation",
        description="Synchronize Geometry Rotation across all layers",
        default=False
    )

    sync_resolution_u: BoolProperty(
        name="Sync Resolution U",
        description="Synchronize Resolution U across all layers",
        default=False
    )

    sync_bevel_resolution: BoolProperty(
        name="Sync Bevel Resolution",
        description="Synchronize Bevel Resolution across all layers",
        default=False
    )
    
    sync_fill_mode: BoolProperty(
        name="Sync Fill Mode",
        description="Synchronize Fill Mode across all layers",
        default=False
    )
    
    sync_auto_adjust_layers: BoolProperty(
        name="Sync Auto-Adjust Layers",
        description="Synchronize Auto-Adjust Layers setting across all layers",
        default=True
    )
    
    sync_z_position: BoolProperty(
        name="Sync Z Position",
        description="Synchronize Z Position across all layers",
        default=False
    )
    
    is_exporting: BoolProperty(
        name="Is Exporting",
        description="Flag to prevent updates during export",
        default=False,
        options={'HIDDEN'}
    )
    
    # Export Settings
    export_folder_path: StringProperty(
        name="Export Location", 
        description="Path to export GLB file",
        default="",
        maxlen=1024,
#        subtype='FILE_PATH'
    )

    export_mode: EnumProperty(
        name="Export Mode",
        description="Choose export mode",
        items=[
            ('SINGLE', "Single", "Export current scene layers"),
            ('BATCH', "Batch", "Batch export multiple files")
        ],
        default='SINGLE'
    )

    export_filename: StringProperty(
        name="Export Filename",
        description="Name for the exported GLB file",
        default="Export.glb",
        maxlen=255
    )


    # Progress tracking properties
    is_processing: BoolProperty(
        name="Is Processing",
        description="Indicates if batch processing is active",
        default=False
    )

    progress_current: IntProperty(
        name="Current Progress",
        description="Current file being processed",
        default=0,
        min=0
    )

    progress_total: IntProperty(
        name="Total Files",
        description="Total files to process",
        default=0,
        min=0
    )

    progress_filename: StringProperty(
        name="Current File",
        description="Current file being processed",
        default=""
    )

    progress_material: StringProperty(
        name="Current Material",
        description="Current material being processed",
        default=""
    )

    progress_factor: FloatProperty(
        name="Progress",
        description="Current progress",
        default=0.0,
        min=0.0,
        max=1.0,
        subtype='FACTOR'
    )

    progress_percentage: IntProperty(
        name="Progress",
        description="Current progress percentage",
        default=0,
        min=0,
        max=100,
        subtype='PERCENTAGE'
    )
    
    # Preset system properties
    presets: EnumProperty(
        name="Presets",
        description="Select a preset to load",
        items=lambda self, context: self.get_preset_items(context),
        update=lambda self, context: self.load_preset(context)
    )

    preset_name: StringProperty(
        name="Preset Name",
        description="Name for the new preset",
        default="My Preset"
    )

    current_preset: StringProperty(
        name="Current Preset",
        description="Currently active preset",
        default=""
    )

    loading_preset: BoolProperty(default=False, options={'HIDDEN'})
    
    def get_preset_items(self, context):
        """Get available presets with layer count"""
        items = [('NONE', "Select Preset", "Select a preset to load")]
        
        preset_dir = self.get_preset_directory()
        if preset_dir.exists():
            preset_files = sorted(preset_dir.glob("*.json"))
            for preset_file in preset_files:
                try:
                    with open(preset_file, 'r') as f:
                        data = json.load(f)
                        layer_count = len(data.get('layers', []))
                        # Display name includes count, but internal ID doesn't
                        display_name = f"{preset_file.stem} ({layer_count})"
                        items.append((preset_file.stem, display_name, f"Load preset: {display_name}"))
                except:
                    # Fallback if file can't be read
                    items.append((preset_file.stem, preset_file.stem, f"Load preset: {preset_file.stem}"))
        
        return items

    def get_preset_directory(self):
        """Get the preset directory path"""
        import bpy
        from pathlib import Path
        
        user_path = Path(bpy.utils.resource_path('USER'))
        preset_path = user_path / "scripts" / "presets" / "svg_layers"
        preset_path.mkdir(parents=True, exist_ok=True)
        
        return preset_path

    def save_preset(self, preset_name):
        """Save current layer settings as preset"""
        preset_data = {
            'layers': [],
            'global_settings': {
                'sync_ambient_occlusion': self.sync_ambient_occlusion,
                'sync_principled_bsdf': self.sync_principled_bsdf,
                'sync_uv_unwrap': self.sync_uv_unwrap,
                'sync_baking': self.sync_baking,
                'sync_material_mode': self.sync_material_mode,
                'sync_layer_offset': self.sync_layer_offset,
                'sync_bevel_depth': self.sync_bevel_depth,
                'sync_geometry_offset': self.sync_geometry_offset,
                'sync_geometry_rotation': self.sync_geometry_rotation,
                'sync_resolution_u': self.sync_resolution_u,
                'sync_bevel_resolution': self.sync_bevel_resolution,
                'sync_fill_mode': self.sync_fill_mode,
                'sync_auto_adjust_layers': self.sync_auto_adjust_layers
            }
        }
        
        # Save each layer's settings - ALL properties
        for layer_setting in self.layer_settings:
            layer_data = {
                # [All the properties remain the same as before]
                'layer_name': layer_setting.layer_name,
                # Visibility states
                'show_layer': layer_setting.show_layer,
                'show_expanded': layer_setting.show_expanded,
                'show_curve_expanded': layer_setting.show_curve_expanded,
                'show_materials_expanded': layer_setting.show_materials_expanded,
                'show_ao_settings': layer_setting.show_ao_settings,
                'show_uv_settings': layer_setting.show_uv_settings,
                'show_packing_settings': layer_setting.show_packing_settings,
                'show_baking_settings': layer_setting.show_baking_settings,
                
                # Geometry settings
                'extrusion_depth': layer_setting.extrusion_depth,
                'bevel_depth': layer_setting.bevel_depth,
                'use_curve_offset': layer_setting.use_curve_offset,
                'geometry_rotation_last': layer_setting.geometry_rotation_last,
                'geometry_rotation': layer_setting.geometry_rotation,
                'fill_mode': layer_setting.fill_mode,
                'auto_adjust_layers': layer_setting.auto_adjust_layers,
                
                # Layer offset
                'layer_offset_percentage': layer_setting.layer_offset_percentage,
                'layer_z_position': layer_setting.layer_z_position,
                
                
                # Curve resolution
                'resolution_u': layer_setting.resolution_u,
                'bevel_resolution': layer_setting.bevel_resolution,
                
                # Points redistribution
                'enable_points_redistribution': layer_setting.enable_points_redistribution,
                'point_spacing': layer_setting.point_spacing,
                'straight_removal': layer_setting.straight_removal,
                'straight_edge_tolerance': layer_setting.straight_edge_tolerance,
                'stored_curve_data': layer_setting.stored_curve_data,
                
                # Materials
                'material_mode': layer_setting.material_mode,
                'use_ambient_occlusion': layer_setting.use_ambient_occlusion,
                'ao_samples': layer_setting.ao_samples,
                'ao_distance': layer_setting.ao_distance,
                'material_metallic': layer_setting.material_metallic,
                'material_roughness': layer_setting.material_roughness,
                
                # UV Unwrap
                'uv_method': layer_setting.uv_method,
                'uv_angle_limit': layer_setting.uv_angle_limit,
                'uv_margin_method': layer_setting.uv_margin_method,
                'uv_rotate_method': layer_setting.uv_rotate_method,
                'uv_island_margin': layer_setting.uv_island_margin,
                'uv_area_weight': layer_setting.uv_area_weight,
                'uv_correct_aspect': layer_setting.uv_correct_aspect,
                'uv_scale_to_bounds': layer_setting.uv_scale_to_bounds,
                
                # MOF settings
                'mof_separate_hard_edges': layer_setting.mof_separate_hard_edges,
                'mof_separate_marked_edges': layer_setting.mof_separate_marked_edges,
                'mof_overlap_identical': layer_setting.mof_overlap_identical,
                'mof_overlap_mirrored': layer_setting.mof_overlap_mirrored,
                'mof_world_scale': layer_setting.mof_world_scale,
                'mof_use_normals': layer_setting.mof_use_normals,
                'mof_suppress_validation': layer_setting.mof_suppress_validation,
                'mof_smooth': layer_setting.mof_smooth,
                'mof_keep_original': layer_setting.mof_keep_original,
                'mof_triangulate': layer_setting.mof_triangulate,
                
                # Cube Projection
                'cube_size': layer_setting.cube_size,
                'cube_correct_aspect': layer_setting.cube_correct_aspect,
                'cube_clip_to_bounds': layer_setting.cube_clip_to_bounds,
                'cube_scale_to_bounds': layer_setting.cube_scale_to_bounds,
                
                # Packing
                'enable_uv_packing': layer_setting.enable_uv_packing,
                'pack_shape_method': layer_setting.pack_shape_method,
                'pack_scale': layer_setting.pack_scale,
                'pack_rotate': layer_setting.pack_rotate,
#                'pack_rotation_method': layer_setting.pack_rotation_method,
                'pack_margin_method': layer_setting.pack_margin_method,
                'pack_margin': layer_setting.pack_margin,
                'pack_pin_islands': layer_setting.pack_pin_islands,
                'pack_pin_method': layer_setting.pack_pin_method,
                'pack_merge_overlapping': layer_setting.pack_merge_overlapping,
                'pack_udim_source': layer_setting.pack_udim_source,
                
                # Baking
                'bake_method': layer_setting.bake_method,
                'bake_samples': layer_setting.bake_samples,
                'bake_margin': layer_setting.bake_margin,
                'texture_resolution': layer_setting.texture_resolution
            }
            preset_data['layers'].append(layer_data)
    
        # Save to file WITHOUT layer count in filename (just internal storage)
        preset_dir = self.get_preset_directory()
        preset_file = preset_dir / f"{preset_name}.json"
        
        with open(preset_file, 'w') as f:
            json.dump(preset_data, f, indent=4)
        
        return preset_file

    def load_preset(self, context):
        """Load selected preset"""
        if self.presets == 'NONE':
            return

        preset_dir = self.get_preset_directory()
        preset_file = preset_dir / f"{self.presets}.json"
        if not preset_file.exists():
            return

        with open(preset_file, 'r') as f:
            preset_data = json.load(f)

        self.loading_preset = True  # keep TRUE until the very end
        try:
            # Clear existing layer settings
            self.layer_settings.clear()

            # Load global settings
            for key, value in preset_data.get('global_settings', {}).items():
                if hasattr(self, key):
                    setattr(self, key, value)

            # Load each layer (setattr updates are safe while loading_preset is True)
            for layer_data in preset_data.get('layers', []):
                new_layer = self.layer_settings.add()
                for key, value in layer_data.items():
                    if hasattr(new_layer, key):
                        setattr(new_layer, key, value)

            # Keep the chosen preset shown
            self.current_preset = self.presets

            # Optional: post-load apply pass (still under loading_preset=True)
            for setting in self.layer_settings:
                setting.update_curve_settings(context)
                setting.update_bevel_depth(context)
                setting.update_offset_only(context)
                setting.update_fill_mode(context)
                setting.update_shape(context)            # solves extrude against target Z
                setting.update_layer_positions(context)  # stacks layers

            # Force UI redraw (guard in case context.screen is None)
            if getattr(context, "screen", None):
                for area in context.screen.areas:
                    area.tag_redraw()

        finally:
            self.loading_preset = False  # CLEAR FLAG only after everything above

    def delete_preset(self, preset_name):
        """Delete a preset file"""
        preset_dir = self.get_preset_directory()
        preset_file = preset_dir / f"{preset_name}.json"
        
        if preset_file.exists():
            preset_file.unlink()
            return True
        return False


class ApplyPointsRedistributionOperator(Operator):
    """Apply points redistribution to the layer's curve"""
    bl_idname = "svg_layers.apply_points_redistribution"
    bl_label = "Apply"
    bl_description = "Apply points redistribution to the curve"
    
    layer_name: StringProperty()
    
    def execute(self, context):
        props = context.scene.svg_layers_props
        
        layer_setting = None
        for settings in props.layer_settings:
            if settings.layer_name == self.layer_name:
                layer_setting = settings
                break
        
        if not layer_setting:
            self.report({'ERROR'}, "Layer settings not found")
            return {'CANCELLED'}
        
        # Get the curve object
        obj = bpy.data.objects.get(self.layer_name)
        if not obj or obj.type != 'CURVE':
            self.report({'ERROR'}, "Curve object not found")
            return {'CANCELLED'}
        
        # ALWAYS store fresh curve data when applying redistribution
        # First, temporarily un-rotate the geometry to store it in neutral state
        if layer_setting.geometry_rotation != 0:
            # Un-rotate the curve
            radians = math.radians(-layer_setting.geometry_rotation)
            cos_r = math.cos(radians)
            sin_r = math.sin(radians)
            
            for spline in obj.data.splines:
                if spline.type == 'BEZIER':
                    for point in spline.bezier_points:
                        x, y = point.co.x, point.co.y
                        point.co.x = x * cos_r - y * sin_r
                        point.co.y = x * sin_r + y * cos_r
                        
                        x, y = point.handle_left.x, point.handle_left.y
                        point.handle_left.x = x * cos_r - y * sin_r
                        point.handle_left.y = x * sin_r + y * cos_r
                        
                        x, y = point.handle_right.x, point.handle_right.y
                        point.handle_right.x = x * cos_r - y * sin_r
                        point.handle_right.y = x * sin_r + y * cos_r
                elif spline.type in ['POLY', 'NURBS']:
                    for point in spline.points:
                        x, y = point.co.x, point.co.y
                        point.co.x = x * cos_r - y * sin_r
                        point.co.y = x * sin_r + y * cos_r

        # Store the unrotated curve
        stored_data = store_curve_data(obj)

        # Re-rotate back to original rotation
        if layer_setting.geometry_rotation != 0:
            radians = math.radians(layer_setting.geometry_rotation)
            cos_r = math.cos(radians)
            sin_r = math.sin(radians)
            
            for spline in obj.data.splines:
                if spline.type == 'BEZIER':
                    for point in spline.bezier_points:
                        x, y = point.co.x, point.co.y
                        point.co.x = x * cos_r - y * sin_r
                        point.co.y = x * sin_r + y * cos_r
                        
                        x, y = point.handle_left.x, point.handle_left.y
                        point.handle_left.x = x * cos_r - y * sin_r
                        point.handle_left.y = x * sin_r + y * cos_r
                        
                        x, y = point.handle_right.x, point.handle_right.y
                        point.handle_right.x = x * cos_r - y * sin_r
                        point.handle_right.y = x * sin_r + y * cos_r
                elif spline.type in ['POLY', 'NURBS']:
                    for point in spline.points:
                        x, y = point.co.x, point.co.y
                        point.co.x = x * cos_r - y * sin_r
                        point.co.y = x * sin_r + y * cos_r

        # Now convert and store the JSON data
        if stored_data:
            import json
            # Convert Vectors to lists for JSON serialization
            json_data = {
                'splines': []
            }
            for spline in stored_data['splines']:
                spline_json = {
                    'type': spline['type'],
                    'use_cyclic_u': spline['use_cyclic_u'],
                    'points': []
                }
                for point in spline['points']:
                    if 'handle_left' in point:
                        # Bezier point
                        spline_json['points'].append({
                            'co': list(point['co']),
                            'handle_left': list(point['handle_left']),
                            'handle_right': list(point['handle_right']),
                            'handle_left_type': point['handle_left_type'],
                            'handle_right_type': point['handle_right_type']
                        })
                    else:
                        # Poly/NURBS point
                        spline_json['points'].append({
                            'co': list(point['co'])
                        })
                json_data['splines'].append(spline_json)
            
            layer_setting.stored_curve_data = json.dumps(json_data)
            
        # Apply redistribution
        success = resample_curve(
            obj,
            layer_setting.point_spacing,
            layer_setting.straight_removal,
            layer_setting.straight_edge_tolerance
        )

        if success:
            self.report({'INFO'}, f"Points redistribution applied to {self.layer_name}")
        else:
            self.report({'WARNING'}, f"Failed to apply redistribution to {self.layer_name}")

        context.view_layer.update()
        return {'FINISHED'}


class SelectTestingFolderOperator(Operator):
    """Browse and select folder for Testing Model"""
    bl_idname = "svg_layers.select_testing_folder"
    bl_label = "Browse Folder"
    
    directory: StringProperty(
        name="Directory",
        subtype='DIR_PATH',
    )
    
    def execute(self, context):
        context.scene.svg_layers_props.testing_folder_path = self.directory
        self.report({'INFO'}, f"Selected: {self.directory}")
        return {'FINISHED'}
    
    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}


class ClearTestingFolderOperator(Operator):
    """Clear the selected folder"""
    bl_idname = "svg_layers.clear_testing_folder"
    bl_label = "Clear Folder"
    bl_description = "Clear the selected folder"
    bl_options = {'INTERNAL'}
    
    def execute(self, context):
        context.scene.svg_layers_props.testing_folder_path = ""
        self.report({'INFO'}, "Folder cleared")
        return {'FINISHED'}

class SelectExportFileOperator(Operator):
    """Browse and select export location with filename"""
    bl_idname = "svg_layers.select_export_file"
    bl_label = "Select Export File"
    
    filepath: StringProperty(
        name="File Path",
        subtype='FILE_PATH',
        default="Export.glb"
    )
    
    def execute(self, context):
        context.scene.svg_layers_props.export_folder_path = self.filepath
        return {'FINISHED'}
    
    def invoke(self, context, event):
        # Generate the default filename
        props = context.scene.svg_layers_props
        if props.testing_folder_path:
            base_name = Path(props.testing_folder_path).name
        else:
            base_name = "Export"
        
        # Set the filepath with the generated name
        props = context.scene.svg_layers_props
        if props.export_folder_path:
            # If there's already a path, use its directory
            existing_path = Path(props.export_folder_path)
            if existing_path.is_dir():
                self.filepath = str(existing_path / f"{base_name}.glb")
            else:
                self.filepath = str(existing_path.parent / f"{base_name}.glb")
        else:
            # No existing path, just use the filename
            self.filepath = f"{base_name}.glb"
        
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}
    
    def get_export_name_from_collections(self):
        """Extract base name from collection names"""
        import re
        
        for collection in bpy.data.collections:
            if collection.name in ["Collection", "Scene Collection"]:
                continue
            
            match = re.match(r'\d+_([^_]+)_\d+', collection.name)
            if match:
                name = match.group(1)
                return name.capitalize()
        
        return None

class ImportTestingSVGsOperator(Operator):
    """Import SVG files from the selected folder"""
    bl_idname = "svg_layers.import_testing_svgs"
    bl_label = "Import SVGs"
    bl_description = "Import all SVG files from the selected folder"
    
    def add_layer_settings(self, context, layer_name):
        """Add geometry settings for a layer if not already exists"""
        props = context.scene.svg_layers_props
        
        # Check if settings already exist for this layer
        for settings in props.layer_settings:
            if settings.layer_name == layer_name:
                return  # Already exists
        
        # Add new settings
        new_settings = props.layer_settings.add()
        new_settings.layer_name = layer_name
        new_settings.layer_z_position = 0.0
    
    def execute(self, context):
        props = context.scene.svg_layers_props
        folder_path = Path(props.testing_folder_path)
        
        # Before any SVG imports, store existing materials
        existing_materials = set(mat.name for mat in bpy.data.materials)
        
        # Validation
        if not props.testing_folder_path:
            self.report({'WARNING'}, "Please select a folder first")
            return {'CANCELLED'}
        
        if not folder_path.exists():
            self.report({'ERROR'}, "Selected folder does not exist")
            return {'CANCELLED'}
        
        # Find all SVG files
        svg_files = []
        for file in folder_path.iterdir():
            if file.suffix.lower() == '.svg':
                svg_files.append(file)
        
        if not svg_files:
            self.report({'WARNING'}, "No SVG files found in folder")
            return {'CANCELLED'}
        
        # Sort files by name
        svg_files.sort()
        
        # Store all created layer objects for scaling
        all_layer_objects = []
        
        # Import each SVG file
        imported_count = 0
        for svg_file in svg_files:
            try:
                # Store existing collections before import
                existing_collections = set(bpy.data.collections.keys())
                
                # Import the SVG file
                bpy.ops.import_curve.svg(filepath=str(svg_file))
                
                # Find the new collection that was created
                new_collections = set(bpy.data.collections.keys()) - existing_collections
                
                # Process each new collection
                for collection_name in new_collections:
                    collection = bpy.data.collections[collection_name]
                    
                    # Extract layer number from the original SVG filename, not collection name
                    # Get just the filename without path
                    svg_filename = svg_file.stem  # This gets filename without .svg extension
                    
                    # Extract numbers from the original filename
                    matches = re.findall(r'(\d+)', svg_filename)
                    if matches:
                        # Take the last number found from the filename
                        layer_number = int(matches[-1])
                    else:
                        # If no number found, use a default
                        layer_number = imported_count + 1
                    
                    # Get all curve objects in this collection
                    curve_objects = [obj for obj in collection.objects if obj.type == 'CURVE']
                    
                    if len(curve_objects) > 1:
                        # Select all curves in this collection
                        bpy.ops.object.select_all(action='DESELECT')
                        for obj in curve_objects:
                            obj.select_set(True)
                        
                        # Set the first curve as active
                        context.view_layer.objects.active = curve_objects[0]
                        
                        # Join all curves into one
                        bpy.ops.object.join()
                        
                        # Get the joined curve
                        joined_curve = context.view_layer.objects.active
                        
                        # Rename the joined curve
                        joined_curve.name = f"Layer {layer_number}"
                        
                        # Store for later scaling
                        all_layer_objects.append(joined_curve)
                        
                        # Add geometry settings for this layer
                        self.add_layer_settings(context, f"Layer {layer_number}")
                        
                        self.report({'INFO'}, f"Joined curves in {collection_name} -> Layer {layer_number}")
                        
                    elif len(curve_objects) == 1:
                        # Rename the single curve
                        curve_objects[0].name = f"Layer {layer_number}"
                        
                        # Store for later scaling
                        all_layer_objects.append(curve_objects[0])
                        
                        # Add geometry settings for this layer
                        self.add_layer_settings(context, f"Layer {layer_number}")
                        
                        self.report({'INFO'}, f"Renamed curve in {collection_name} -> Layer {layer_number}")
                
                imported_count += 1
                
            except Exception as e:
                self.report({'ERROR'}, f"Failed to import {svg_file.name}: {str(e)}")
                continue
        
        # After all joining, remove only unused SVG materials
        for mat in list(bpy.data.materials):
            if mat.users == 0 and mat.name not in existing_materials:
                bpy.data.materials.remove(mat)
        
        # After all imports, scale all layers together based on Layer 1
        if all_layer_objects:
            # Find Layer 1
            layer_1 = None
            for obj in all_layer_objects:
                if obj.name == "Layer 1":
                    layer_1 = obj
                    break
            
            if layer_1:
                # Calculate scale factor based on Layer 1's largest XY dimension
                max_dim = max(layer_1.dimensions.x, layer_1.dimensions.y)
                if max_dim > 0:
                    scale_factor = 1.0 / max_dim
                    
                    # Select all layer objects
                    bpy.ops.object.select_all(action='DESELECT')
                    for obj in all_layer_objects:
                        obj.select_set(True)
                    
                    # Make Layer 1 the active object (last selected)
                    context.view_layer.objects.active = layer_1
                    
                    # Scale all selected objects together
                    for obj in all_layer_objects:
                        obj.scale = (scale_factor, scale_factor, scale_factor)

                    # Apply scale transform to all
                    bpy.ops.object.select_all(action='DESELECT')
                    for obj in all_layer_objects:
                        obj.select_set(True)
                        context.view_layer.objects.active = obj
                        bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)

                    # Set mean radius to 1.000 for all curve points
                    for obj in all_layer_objects:
                        if obj.type == 'CURVE':
                            # Select only this object
                            bpy.ops.object.select_all(action='DESELECT')
                            obj.select_set(True)
                            context.view_layer.objects.active = obj
                            
                            # Enter edit mode
                            bpy.ops.object.mode_set(mode='EDIT')
                            
                            # Select all points
                            bpy.ops.curve.select_all(action='SELECT')
                            
                            # Set mean radius to 1.0
                            bpy.ops.curve.radius_set(radius=1.0)
                            
                            # Return to object mode
                            bpy.ops.object.mode_set(mode='OBJECT')

                    self.report({'INFO'}, f"Scaled all layers to Layer 1 = 1m (scale factor: {scale_factor:.3f})")
                    self.report({'INFO'}, "Applied scale and set mean radius to 1.0 for all curves")
        
        # Reset 3D cursor to world origin
        bpy.context.scene.cursor.location = (0, 0, 0)

        # Find Layer 1 and set origin to geometry center
        layer_1 = None
        for obj in all_layer_objects:
            if obj.name == "Layer 1":
                layer_1 = obj
                break

        if layer_1:
            # Store Layer 1's position before origin change
            old_position = layer_1.location.copy()

            # Set Layer 1's origin to its geometry center
            bpy.ops.object.select_all(action='DESELECT')
            layer_1.select_set(True)
            bpy.context.view_layer.objects.active = layer_1
            bpy.ops.object.origin_set(type='ORIGIN_GEOMETRY', center='MEDIAN')

            # Calculate how much Layer 1 needs to move to reach origin
            movement_vector = Vector((0, 0, 0)) - layer_1.location

            # Move Layer 1 to world origin
            layer_1.location = (0, 0, 0)

            # Move all other layers by the same amount
            for obj in all_layer_objects:
                if obj != layer_1:
                    obj.location = obj.location + movement_vector
        
        # Set origin to geometry for all layers (Layer 1 already done)
        for obj in all_layer_objects:
            if obj.name != "Layer 1":  # Skip Layer 1 as it's already done
                bpy.ops.object.select_all(action='DESELECT')
                obj.select_set(True)
                bpy.context.view_layer.objects.active = obj
                bpy.ops.object.origin_set(type='ORIGIN_GEOMETRY', center='MEDIAN')
        
        # Add Edge Split modifier to all imported layers
        for obj in all_layer_objects:
            obj.modifiers.new(name="Edge Split", type='EDGE_SPLIT')
        
        # Enable nodes for all layers and preserve their colors
        for obj in all_layer_objects:
            if obj.data.materials and obj.data.materials[0]:
                mat = obj.data.materials[0]
                
                # Store original diffuse color BEFORE enabling nodes
                original_color = mat.diffuse_color[:4]
                
                # Rename material to match layer name
                mat.name = obj.name
                
                # Enable nodes (this resets the material)
                mat.use_nodes = True
                
                # Find and update Principled BSDF with original color
                for node in mat.node_tree.nodes:
                    if node.type == 'BSDF_PRINCIPLED':
                        node.inputs['Base Color'].default_value = original_color
                        break
        
        # Apply all existing settings to newly imported layers ONLY if a preset is selected
        if props.presets != 'NONE':
            # Set flag to prevent preset from being reset
            props.loading_preset = True
            
            for obj in all_layer_objects:
                for settings in props.layer_settings:
                    if settings.layer_name == obj.name:
                        # Reset geometry rotation tracking before applying
                        settings.geometry_rotation_last = 0
                        
                        # Apply all geometry settings
                        settings.update_shape(context)
                        settings.update_curve_settings(context)
                        settings.update_bevel_depth(context)
                        settings.update_offset_only(context)
                        settings.update_fill_mode(context)
                        
                        # Initialize Z position based on current position
                        obj = bpy.data.objects.get(settings.layer_name)
                        if obj:
                            settings.layer_z_position = obj.location.z
                        
                        # Apply geometry rotation if set
                        if settings.geometry_rotation != 0:
                            # Since we reset geometry_rotation_last to 0, this will apply the full rotation
                            settings.update_geometry_rotation(context)
                        
                        # Apply points redistribution if enabled
                        if settings.enable_points_redistribution:
                            resample_curve(
                                obj,
                                settings.point_spacing,
                                settings.straight_removal,
                                settings.straight_edge_tolerance
                            )
                        
                        # Finally update positions
                        settings.update_layer_positions(context)
                        break
            
            # Clear the flag after ALL operations are complete
            props.loading_preset = False

        # Final viewport update
        context.view_layer.update()
        
        self.report({'INFO'}, f"Successfully imported and processed {imported_count} SVG files")
        
        # Ensure no layers are expanded after import
        for layer_setting in props.layer_settings:
            layer_setting.show_layer = False
            layer_setting.show_expanded = False
            layer_setting.show_curve_expanded = False
            layer_setting.show_materials_expanded = False

        # Deselect all objects to prevent auto-expansion
        bpy.ops.object.select_all(action='DESELECT')

        # Force UI redraw to show the imported layers
        for area in context.screen.areas:
            if area.type == 'VIEW_3D':
                area.tag_redraw()
                
        return {'FINISHED'}


class ClearExportFolderOperator(Operator):
    """Clear the export folder path"""
    bl_idname = "svg_layers.clear_export_folder"
    bl_label = "Clear Export Folder"
    bl_description = "Clear the export folder path"
    bl_options = {'INTERNAL'}
    
    def execute(self, context):
        context.scene.svg_layers_props.export_folder_path = ""
        return {'FINISHED'}


class ExportLayersGLBOperator(Operator):
    """Export all layers as GLB file"""
    bl_idname = "svg_layers.export_glb"
    bl_label = "Export GLB"
    bl_description = "Export all layers as a single GLB file"
    
    def execute(self, context):
        # Clean up any leftover MOF processes/files
        cleanup_mof_and_temp_files()
        time.sleep(0.5)
        
        props = context.scene.svg_layers_props 
        props.is_exporting = True
        
        # Set processing flag for UI display
        props.is_processing = True
        props.progress_filename = "SINGLE EXPORT"
        props.progress_current = 0
        props.progress_total = 0
        
        # Store current viewport shading and switch to solid
        stored_shading = None
        for area in context.screen.areas:
            if area.type == 'VIEW_3D':
                for space in area.spaces:
                    if space.type == 'VIEW_3D':
                        stored_shading = space.shading.type
                        space.shading.type = 'SOLID'
                        break
                break
        
        # Validation
        if not props.export_folder_path:
            self.report({'WARNING'}, "Please select an export location first")
            return {'CANCELLED'}

        # Parse the file path
        export_file_path = Path(props.export_folder_path)
        export_path = export_file_path.parent  # Get the directory
        filename = export_file_path.name if export_file_path.suffix == '.glb' else None

        # If no filename was provided or it doesn't end with .glb, generate one
        if not filename:
            base_name = self.get_export_name_from_folder()
            if not base_name:
                base_name = "Export"
            filename = f"{base_name}.glb"

        # Full export path
        full_export_path = export_path / filename
        
        # Find all layer objects
        layer_objects = []
        for layer_setting in props.layer_settings:
            obj = bpy.data.objects.get(layer_setting.layer_name)
            if obj:
                layer_objects.append(obj)
        
        if not layer_objects:
            self.report({'WARNING'}, "No layers found to export")
            return {'CANCELLED'}
        
        # Parse the file path to get filename if provided
        export_file_path = Path(props.export_folder_path)
        if export_file_path.suffix == '.glb':
            # User provided a full path with filename
            export_path = export_file_path.parent
            filename = export_file_path.name
            full_export_path = export_file_path
        else:
            # User only selected a folder, generate filename from collection
            export_path = export_file_path if export_file_path.is_dir() else export_file_path.parent
            base_name = self.get_export_name_from_folder()
            if not base_name:
                base_name = "Export"
            filename = f"{base_name}.glb"
            full_export_path = export_path / filename
            
            # Check if file exists and generate unique name if needed
            if full_export_path.exists():
                base_name = filename[:-4] if filename.lower().endswith('.glb') else filename
                filename = self.get_unique_filename(export_path, base_name)
                full_export_path = export_path / filename
        
        # Store original visibility states
        original_states = []
        for obj in layer_objects:
            original_states.append({'obj': obj, 'hide_viewport': obj.hide_viewport, 'hide_render': obj.hide_render})
        
        mof_extract_path = None
        mof_exe_path = None
        mof_needed = any((s.uv_method == 'MOF' and s.use_ambient_occlusion) for s in props.layer_settings)
        if mof_needed:
            addon_dir = os.path.dirname(os.path.realpath(__file__))
            mof_zip_path = os.path.join(addon_dir, "resources", "MinistryOfFlat_Release.zip")
            if os.path.exists(mof_zip_path):
                try:
                    mof_extract_path = tempfile.mkdtemp(prefix="single_mof_")
                    with zipfile.ZipFile(mof_zip_path, 'r') as zip_ref:
                        zip_ref.extractall(mof_extract_path)
                    for root, dirs, files in os.walk(mof_extract_path):
                        for file in files:
                            if file.lower() == "unwrapconsole3.exe":
                                mof_exe_path = os.path.join(root, file)
                                break
                        if mof_exe_path:
                            break
                    if mof_exe_path:
                        context._single_mof_exe = mof_exe_path
                        context._single_mof_extract_path = mof_extract_path
                except Exception:
                    pass
        
        try:
            # Make all layers visible for duplication
            for obj in layer_objects:
                obj.hide_viewport = False
                obj.hide_render = False
                        
            # Select all layer objects
            bpy.ops.object.select_all(action='DESELECT')
            for obj in layer_objects:
                obj.select_set(True)
            
            # Set Layer 1 as active if it exists
            layer_1 = None
            for obj in layer_objects:
                if obj.name == "Layer 1":
                    layer_1 = obj
                    break
            
            if layer_1:
                context.view_layer.objects.active = layer_1
            else:
                context.view_layer.objects.active = layer_objects[0]
            
            # Duplicate all selected objects at once
            bpy.ops.object.duplicate_move(OBJECT_OT_duplicate={"linked":False})

            # Get the duplicated objects (they are now selected)
            temp_objects = context.selected_objects.copy()
            
            settings_by_name = {s.layer_name: s for s in props.layer_settings}
            
            # Process each object individually
            for obj in temp_objects:
                if obj.data.materials and obj.data.materials[0]:
                    if obj.data.materials[0].users > 1:
                        obj.data.materials[0] = obj.data.materials[0].copy()
                
                bpy.ops.object.select_all(action='DESELECT')
                obj.select_set(True)
                context.view_layer.objects.active = obj
                
                # Convert to mesh if it's a curve
                if obj.type == 'CURVE':
                    # Remove Edge Split modifier before conversion
                    for modifier in list(obj.modifiers):
                        if modifier.type == 'EDGE_SPLIT':
                            obj.modifiers.remove(modifier)
                    
                    # Convert to mesh
                    bpy.ops.object.convert(target='MESH')
                    print(f"[FIRST EXPORT CHECK] Converted {obj.name} to mesh")

                    # Apply transforms (first time)
                    bpy.ops.object.transform_apply(location=False, rotation=True, scale=True)
                    print(f"[FIRST EXPORT CHECK] Applied transforms (1st) for {obj.name}")

                    # Merge vertices by distance
                    merge_by_distance_bmesh(obj, threshold=0.000001)
                    print(f"[FIRST EXPORT CHECK] Merged vertices for {obj.name}")

                    # Reset normal vectors
                    if obj.data.has_custom_normals:
                        obj.data.normals_split_custom_set([])

                    # Update normals using the new API in Blender 4.5
                    obj.data.update()  # This updates the mesh including normals
            
            # Select all temp objects and recollect them by name
            bpy.ops.object.select_all(action='DESELECT')
            temp_objects_names = [obj.name for obj in temp_objects]
            temp_objects = []
            for name in temp_objects_names:
                obj = bpy.data.objects.get(name)
                if obj:
                    obj.select_set(True)
                    temp_objects.append(obj)
            
            # Set active to Layer 1 copy if exists
            layer_1_temp = None
            for obj in temp_objects:
                if "Layer 1" in obj.name:
                    layer_1_temp = obj
                    context.view_layer.objects.active = obj
                    break
            
            if not layer_1_temp and temp_objects:
                context.view_layer.objects.active = temp_objects[0]
            
            # Rotate everything 90 degrees around X axis
            for obj in temp_objects:
                original_loc = obj.location.copy()
                obj.rotation_euler[0] = math.radians(90)
                new_loc = obj.location.copy()
                new_loc.y = -original_loc.z
                new_loc.z = original_loc.y
                obj.location = new_loc
            
            # Apply UV unwrap and baking for EACH layer that has AO enabled       
            # First, hide all ORIGINAL layer objects to prevent shadow baking
            for layer_setting in props.layer_settings:
                original_obj = bpy.data.objects.get(layer_setting.layer_name)
                if original_obj:
                    original_obj.hide_set(True)
                    original_obj.hide_render = True
            print(f"[DEBUG] Hidden original objects: {[s.layer_name for s in props.layer_settings]}")

            for obj in temp_objects:
                if obj.type == 'MESH':
                    # Find the layer settings for this specific object
                    base_name = obj.name.split('.')[0]
                    layer_setting = settings_by_name.get(base_name)
                    print(f"[DEBUG] Object: {obj.name}, base_name: {base_name}, found settings: {layer_setting is not None}")
                    
                    # Only process if this layer has AO enabled
                    if layer_setting and layer_setting.use_ambient_occlusion:
                        # Apply UV unwrap with THIS layer's settings
                        bpy.ops.object.select_all(action='DESELECT')
                        obj.select_set(True)
                        context.view_layer.objects.active = obj
                        
                        # Clear any existing UV maps
                        bpy.ops.object.mode_set(mode='OBJECT')
                        while obj.data.uv_layers:
                            obj.data.uv_layers.remove(obj.data.uv_layers[0])
                        
                        # Back to edit mode for UV unwrap
                        bpy.ops.object.mode_set(mode='EDIT')
                        bpy.ops.mesh.select_all(action='SELECT')
                        bpy.ops.object.mode_set(mode='OBJECT')
                        
                        print(f"Applying UV unwrap ({layer_setting.uv_method}) to {obj.name} with settings from {layer_setting.layer_name}")
                        ok = apply_uv_unwrap(obj, layer_setting, context)
                        if not ok:
                            print(f"[UV WARN] Skipping bake for {obj.name} (unwrap failed)")
                            continue
                        
                        # Only now add Edge Split and bake
                        edge_split = obj.modifiers.new(name="EdgeSplit", type='EDGE_SPLIT')
                        edge_split.split_angle = math.radians(30)
                        
                        if obj.data.materials:
                            print(f"Baking textures for {obj.name} with settings from {layer_setting.layer_name}")
                            bake_textures_for_layer(obj, layer_setting, context)
            
            # Unhide original objects after baking
            for layer_setting in props.layer_settings:
                original_obj = bpy.data.objects.get(layer_setting.layer_name)
                if original_obj:
                    original_obj.hide_set(False)
                    original_obj.hide_render = False
            
            # Ensure all temp objects are selected for export
            bpy.ops.object.select_all(action='DESELECT')
            for obj in temp_objects:
                obj.select_set(True)
            
            # Export with default settings
            bpy.ops.export_scene.gltf(
                filepath=str(full_export_path),
                use_selection=True,
                export_format='GLB',
                export_apply=True
            )
            
            # Delete temporary objects
            bpy.ops.object.delete(use_global=False)
            
            self.report({'INFO'}, f"Exported to: {full_export_path.name}")
            
        except Exception as e:
            self.report({'ERROR'}, f"Export failed: {str(e)}")
            # Try to clean up any temp objects
            bpy.ops.object.select_all(action='DESELECT')
            for obj in bpy.data.objects:
                if "_export_temp" in obj.name or ".001" in obj.name:
                    obj.select_set(True)
            if context.selected_objects:
                bpy.ops.object.delete(use_global=False)
            return {'CANCELLED'}
        
        finally:
            props = context.scene.svg_layers_props
            props.is_exporting = False
            
            # Clear processing flag
            props.is_processing = False
            props.progress_filename = ""
            
            # Restore viewport shading
            if stored_shading:
                for area in context.screen.areas:
                    if area.type == 'VIEW_3D':
                        for space in area.spaces:
                            if space.type == 'VIEW_3D':
                                space.shading.type = stored_shading
                                break
                        break
            
            # Clean up baked textures and materials
            for img in list(bpy.data.images):
                if "_BaseColor" in img.name or "_Metallic" in img.name or "_Roughness" in img.name or "_AO" in img.name:
                    bpy.data.images.remove(img)
            
            # Clean up cached MOF from single export
            scene = bpy.context.scene
            extract = scene.get('_temp_batch_mof_extract')
            if extract and os.path.exists(extract):
                try:
                    shutil.rmtree(extract)
                except:
                    pass
            for k in ('_temp_batch_mof_exe', '_temp_batch_mof_extract'):
                if k in scene:
                    del scene[k]
                    
            # Clean up glTF Material Output node group
            if "glTF Material Output" in bpy.data.node_groups:
                try:
                    bpy.data.node_groups.remove(bpy.data.node_groups["glTF Material Output"])
                except:
                    pass
            
            # Restore original states
            for state in original_states:
                obj = state['obj']
                if obj.name in bpy.data.objects:
                    obj.hide_viewport = state['hide_viewport']
                    obj.hide_render = state['hide_render']
            
            # Clean up orphan data
            for mesh in list(bpy.data.meshes):
                if mesh.users == 0:
                    bpy.data.meshes.remove(mesh)
            
            # Restore selection
            bpy.ops.object.select_all(action='DESELECT')
            for obj in layer_objects:
                if obj.name in bpy.data.objects:
                    obj.select_set(True)
            
            if layer_objects:
                context.view_layer.objects.active = layer_objects[0]
                
            # Clean up temp MOF exe reference
            if '_temp_batch_mof_exe' in context.scene:
                del context.scene['_temp_batch_mof_exe']
        
        return {'FINISHED'}
    
    def get_export_name_from_folder(self):
        """Get export name from the testing folder path"""
        props = bpy.context.scene.svg_layers_props
        if props.testing_folder_path:
            folder_path = Path(props.testing_folder_path)
            return folder_path.name
        return None
    
    def get_unique_filename(self, folder_path, base_name):
        """Generate unique filename with incrementing suffix if needed"""
        filename = f"{base_name}.glb"
        full_path = folder_path / filename
        
        if not full_path.exists():
            return filename
        
        counter = 2
        while True:
            filename = f"{base_name}_{counter:02d}.glb"
            full_path = folder_path / filename
            if not full_path.exists():
                return filename
            counter += 1


class ExportBatchGLBOperator(Operator):
    """Export all models in bundle folder as GLB files"""
    bl_idname = "svg_layers.export_batch_glb"
    bl_label = "Export Batch"
    bl_description = "Process and export all model folders in the bundle"
    
    # Modal operator properties
    _timer = None
    _model_folders = []
    _current_folder_index = 0
    _total_operations = 0
    _completed_operations = 0
    _stored_settings = None
    _output_folder = None
    _empty_folders = []
    _failed_folders = []
    _is_cancelled = False
    _original_testing_path = ""
    
    _mof_extract_path = None
    _mof_exe_path = None
    
    def modal(self, context, event):
        # Check for cancellation
        if event.type in {'RIGHTMOUSE', 'ESC'} or self._is_cancelled:
            self.cancel(context)
            return {'CANCELLED'}
        
        if event.type == 'TIMER':
            props = context.scene.svg_layers_props
            
            # Process one folder per timer tick
            if self._current_folder_index < len(self._model_folders):
                model_folder = self._model_folders[self._current_folder_index]
                
                # Update progress in header
                progress_text = (f"Processing: {model_folder.name} "
                               f"({self._completed_operations + 1}/{self._total_operations}) "
                               f"-> {self._output_folder.name} "
                               f"- Press ESC to cancel")
                
                # Update progress properties for UI
                props.progress_current = self._completed_operations
                props.progress_total = self._total_operations
                props.progress_filename = model_folder.name
                props.progress_material = ""  # Not using materials in this version
                # Calculate progress based on layers instead of models
                if self._total_layers > 0:
                    layer_progress = self._completed_layers / self._total_layers
                    props.progress_factor = layer_progress
                    props.progress_percentage = int(layer_progress * 100)
                else:
                    props.progress_factor = (self._completed_operations + 1) / self._total_operations
                    props.progress_percentage = int(props.progress_factor * 100)
                
                # Find the 3D viewport area and set header text
                for area in context.screen.areas:
                    if area.type == 'VIEW_3D':
                        area.header_text_set(progress_text)
                        break
                
                # Also update status bar
                context.workspace.status_text_set(progress_text)
                
                # Check if folder contains SVG files
                svg_files = list(model_folder.glob("*.svg"))
                
                if not svg_files:
                    self._empty_folders.append(model_folder.name)
                    self._current_folder_index += 1
                    self._completed_operations += 1
                else:
                    # Process the folder
                    try:
                        print(f"[BATCH DEBUG] Processing folder {self._current_folder_index + 1}/{len(self._model_folders)}: {model_folder.name}")
                        
                        # Clear everything from Blender
                        self.complete_cleanup(context)
                        print(f"[BATCH DEBUG] Cleanup completed")
                        
                        # Set the testing folder path to this model folder
                        props.testing_folder_path = str(model_folder)
                        print(f"[BATCH DEBUG] Set testing path to: {props.testing_folder_path}")
                        
                        # Import SVGs from this folder
                        bpy.ops.svg_layers.import_testing_svgs()
                        # Check if cancelled
                        if self._is_cancelled:
                            return
                        
                        # Apply stored settings to newly imported layers
                        self.apply_stored_settings(context, props, self._stored_settings)
                        print(f"[BATCH DEBUG] Settings applied")
                        
                        # Use folder name as filename
                        base_name = model_folder.name
                        
                        # Get unique filename in the shared output folder
                        filename = self.get_unique_filename(self._output_folder, base_name)
                        full_export_path = self._output_folder / filename
                        
                        # Export using the existing export logic
                        self.export_layers_as_glb(context, props, full_export_path)
                        print(f"[BATCH SUCCESS] Exported {model_folder.name}")
                        
                        # After successful export, update layer count
                        svg_files = list(model_folder.glob("*.svg"))
                        self._completed_layers += len(svg_files)

                        self.report({'INFO'}, f"Exported: {model_folder.name} -> {filename}")
                        
                    except Exception as e:
                        self._failed_folders.append(model_folder.name)
                        print(f"[BATCH ERROR] Exception in modal for {model_folder.name}: {str(e)}")
                        import traceback
                        traceback.print_exc()  # This will show the full error with line numbers
                        self.report({'ERROR'}, f"Failed to process {model_folder.name}: {str(e)}")
                    
                    self._current_folder_index += 1
                    self._completed_operations += 1
                
                # Force redraw
                for area in context.screen.areas:
                    area.tag_redraw()
            else:
                # All done
                self.finish(context)
                return {'FINISHED'}
        
        return {'RUNNING_MODAL'}
    
    def invoke(self, context, event):
        # Clean up any leftover MOF processes/files from previous runs
        cleanup_mof_and_temp_files()
        time.sleep(0.5)
        
        props = context.scene.svg_layers_props
        
        # Collapse all layers in the UI
        for layer_setting in props.layer_settings:
            layer_setting.show_layer = False
            layer_setting.show_expanded = False
            layer_setting.show_offset_expanded = False
            layer_setting.show_curve_expanded = False
        
        # Get current folder (where current SVGs were imported from)
        current_folder = Path(props.testing_folder_path)
        if not current_folder or not current_folder.exists():
            self.report({'ERROR'}, "No valid folder path set")
            return {'CANCELLED'}
        
        # Store original path to restore later
        self._original_testing_path = str(current_folder)
        
        # Go up one level to bundle folder
        bundle_folder = current_folder.parent
        
        # Create unique output folder at bundle level
        self._output_folder = self.get_unique_output_folder(bundle_folder)
        self._output_folder.mkdir(exist_ok=True)
        
        # Store current layer settings from the currently imported model
        self._stored_settings = self.store_all_layer_settings(context, props)
        
        # Extract MOF once for entire batch if any layer uses MOF
        mof_needed = False
        for layer_setting in self._stored_settings['layers']:
            if layer_setting.get('uv_method') == 'MOF' and layer_setting.get('use_ambient_occlusion'):
                mof_needed = True
                break

        if mof_needed:
            print(f"[BATCH DEBUG] MOF needed for batch export")
            addon_dir = os.path.dirname(os.path.realpath(__file__))
            mof_zip_path = os.path.join(addon_dir, "resources", "MinistryOfFlat_Release.zip")
            
            print(f"[BATCH DEBUG] Looking for MOF at: {mof_zip_path}")
            if os.path.exists(mof_zip_path):
                print(f"[BATCH DEBUG] MOF zip found, extracting...")
                self._mof_extract_path = tempfile.mkdtemp(prefix="batch_mof_")
                with zipfile.ZipFile(mof_zip_path, 'r') as zip_ref:
                    zip_ref.extractall(self._mof_extract_path)
                
                # Find exe
                for root, dirs, files in os.walk(self._mof_extract_path):
                    for file in files:
                        if file.lower() == "unwrapconsole3.exe":
                            self._mof_exe_path = os.path.join(root, file)
                            break
                    if self._mof_exe_path:
                        print(f"[BATCH DEBUG] MOF exe found at: {self._mof_exe_path}")
                    else:
                        print(f"[BATCH ERROR] MOF exe not found after extraction!")
                else:
                    print(f"[BATCH ERROR] MOF zip not found at expected location!")                                            
        
        # Get only folders containing SVG files
        self._model_folders = self.get_valid_model_folders(bundle_folder)
        # Filter out the output folder itself
        self._model_folders = [f for f in self._model_folders if f.name != "Processed Models to GLB"]
        self._model_folders.sort()  # Process in alphabetical order
        
        # Count total layers across all models
        self._total_layers = 0
        for model_folder in self._model_folders:
            svg_files = list(model_folder.glob("*.svg"))
            self._total_layers += len(svg_files)
        
        # Initialize counters
        self._total_operations = len(self._model_folders)
        self._completed_operations = 0
        self._current_folder_index = 0
        self._completed_layers = 0
        self._empty_folders = []
        self._failed_folders = []
        self._is_cancelled = False
        
        # Mark processing as active
        props.is_processing = True
        
        # Store viewport shading and switch to solid
        self._stored_shading = None
        for area in context.screen.areas:
            if area.type == 'VIEW_3D':
                for space in area.spaces:
                    if space.type == 'VIEW_3D':
                        self._stored_shading = space.shading.type
                        space.shading.type = 'SOLID'
                        break
                break
        
        self.report({'INFO'}, f"Starting batch export of {self._total_operations} model folders")
        
        # Start modal timer
        wm = context.window_manager
        self._timer = wm.event_timer_add(0.1, window=context.window)  # 0.1 seconds between operations
        wm.modal_handler_add(self)
        
        return {'RUNNING_MODAL'}
    
    def cancel(self, context):
        """Handle cancellation"""
        self._is_cancelled = True
        props = context.scene.svg_layers_props
        props.is_exporting = False
        
        # Restore viewport shading
        if hasattr(self, '_stored_shading') and self._stored_shading:
            for area in context.screen.areas:
                if area.type == 'VIEW_3D':
                    for space in area.spaces:
                        if space.type == 'VIEW_3D':
                            space.shading.type = self._stored_shading
                            break
                    break
        
        # Mark processing as inactive immediately so MOF can detect cancellation
        props.is_processing = False
        
        # Clean up MOF and temp files
        cleanup_mof_and_temp_files()
        
        # Remove timer
        if self._timer:
            context.window_manager.event_timer_remove(self._timer)
            self._timer = None
        
        # Clear header text from all 3D viewports
        for area in context.screen.areas:
            if area.type == 'VIEW_3D':
                area.header_text_set(None)
        
        # Clear status text
        context.workspace.status_text_set(None)
        
        # Mark processing as inactive
        props.is_processing = False
        
        # Clear progress info
        props.progress_current = 0
        props.progress_total = 0
        props.progress_filename = ""
        props.progress_material = ""
        props.progress_factor = 0.0
        props.progress_percentage = 0
        
        # Final cleanup
        self.complete_cleanup(context)
        
        # Restore original testing path and reimport the original model
        if self._original_testing_path:
            props.testing_folder_path = self._original_testing_path
            
            # Try to reimport the original model
            try:
                bpy.ops.svg_layers.import_testing_svgs()
                # Reapply the stored settings
                self.apply_stored_settings(context, props, self._stored_settings)
            except:
                pass  # If reimport fails, just leave it empty
        
        # Clean up MOF at the very end
        if self._mof_extract_path and os.path.exists(self._mof_extract_path):
            try:
                shutil.rmtree(self._mof_extract_path)
            except:
                pass
        
        self.report({'WARNING'}, f"Batch export cancelled. Completed {self._completed_operations}/{self._total_operations} models")
    
    def finish(self, context):
        """Handle successful completion"""
        props = context.scene.svg_layers_props
        props.is_exporting = False
        
        # Restore viewport shading
        if hasattr(self, '_stored_shading') and self._stored_shading:
            for area in context.screen.areas:
                if area.type == 'VIEW_3D':
                    for space in area.spaces:
                        if space.type == 'VIEW_3D':
                            space.shading.type = self._stored_shading
                            break
                    break
        
        # Remove timer
        if self._timer:
            context.window_manager.event_timer_remove(self._timer)
            self._timer = None
        
        # Clear header text from all 3D viewports
        for area in context.screen.areas:
            if area.type == 'VIEW_3D':
                area.header_text_set(None)
        
        # Clear status text
        context.workspace.status_text_set(None)
        
        # Mark processing as inactive
        props.is_processing = False
        
        # Clear progress info
        props.progress_current = 0
        props.progress_total = 0
        props.progress_filename = ""
        props.progress_material = ""
        props.progress_factor = 0.0
        props.progress_percentage = 0
        
        # Final cleanup
        self.complete_cleanup(context)
        
        # Restore original testing path
        if self._original_testing_path:
            props.testing_folder_path = self._original_testing_path
            
            # Try to reimport the original model
            try:
                bpy.ops.svg_layers.import_testing_svgs()
                # Reapply the stored settings
                self.apply_stored_settings(context, props, self._stored_settings)
            except:
                pass  # If reimport fails, just leave it empty
            
        # Clean up MOF at the very end
        if self._mof_extract_path and os.path.exists(self._mof_extract_path):
            try:
                shutil.rmtree(self._mof_extract_path)
            except:
                pass
        
        # Report summary
        successful_count = self._completed_operations - len(self._failed_folders)
        self.report({'INFO'}, f"Batch export complete! Successfully exported {successful_count} models with SVGs to: {self._output_folder.name}")

        if self._failed_folders:
            self.report({'ERROR'}, f"Failed to process {len(self._failed_folders)} folders: {', '.join(self._failed_folders)}")
    
    def execute(self, context):
        props = context.scene.svg_layers_props
        props.is_exporting = True
        
        # For modal operators, execute just calls invoke
        return self.invoke(context, event=None)
    
    def store_all_layer_settings(self, context, props):
        """Store all settings from current layers"""
        settings = {
            'layers': [],
            'global_settings': {}
        }
        
        # Store each layer's settings
        for layer_setting in props.layer_settings:
            layer_data = {
                'layer_name': layer_setting.layer_name,
                'show_layer': layer_setting.show_layer,
                'show_expanded': layer_setting.show_expanded,
                'show_curve_expanded': layer_setting.show_curve_expanded,
                'layer_offset_percentage': layer_setting.layer_offset_percentage,
                'layer_z_position': layer_setting.layer_z_position,
                'auto_adjust_layers': layer_setting.auto_adjust_layers, 
                'fill_mode': layer_setting.fill_mode,  
                'resolution_u': layer_setting.resolution_u,
                'bevel_resolution': layer_setting.bevel_resolution,
                'enable_points_redistribution': layer_setting.enable_points_redistribution,
                'point_spacing': layer_setting.point_spacing,
                'straight_removal': layer_setting.straight_removal,
                'straight_edge_tolerance': layer_setting.straight_edge_tolerance,
                'extrusion_depth': layer_setting.extrusion_depth,
                'bevel_depth': layer_setting.bevel_depth,
                'use_curve_offset': layer_setting.use_curve_offset,
                'geometry_rotation': layer_setting.geometry_rotation,
                'geometry_rotation_last': layer_setting.geometry_rotation_last,
                'show_materials_expanded': layer_setting.show_materials_expanded,
                'material_mode': layer_setting.material_mode,
                'use_ambient_occlusion': layer_setting.use_ambient_occlusion,
                'ao_samples': layer_setting.ao_samples,
                'ao_distance': layer_setting.ao_distance,
                'show_ao_settings': layer_setting.show_ao_settings,
                'material_metallic': layer_setting.material_metallic,
                'material_roughness': layer_setting.material_roughness,
            }
            settings['layers'].append(layer_data)
        
        return settings
    
    def apply_stored_settings(self, context, props, stored_settings):
        """Apply stored settings to newly imported layers"""
        print(f"[SETTINGS DEBUG] Applying settings to {len(props.layer_settings)} layers")
        
        # Match layers by their number (Layer 1, Layer 2, etc.)
        for stored_layer in stored_settings['layers']:
            # Extract layer number from stored layer name
            import re
            match = re.search(r'Layer (\d+)', stored_layer['layer_name'])
            if not match:
                continue
            
            layer_number = match.group(1)
            
            # Find corresponding layer in newly imported model
            for layer_setting in props.layer_settings:
                if f"Layer {layer_number}" == layer_setting.layer_name:
                    # Apply all settings EXCEPT layer_offset_percentage first
#                    layer_setting.show_layer = stored_layer['show_layer']
#                    layer_setting.show_expanded = stored_layer['show_expanded']
#                    layer_setting.show_curve_expanded = stored_layer['show_curve_expanded']
                    layer_setting.auto_adjust_layers = stored_layer.get('auto_adjust_layers', True)
                    layer_setting.auto_adjust_layers = stored_layer.get('auto_adjust_layers', True)
                    # Fill mode is already correct for Blender 4.5 - no conversion needed
                    fill_mode_value = stored_layer.get('fill_mode', 'BOTH')
                    print(f"[SETTINGS DEBUG] Setting fill_mode={fill_mode_value} for {layer_setting.layer_name}")
                    layer_setting.fill_mode = fill_mode_value
                    layer_setting.fill_mode = fill_mode_value
                    layer_setting.resolution_u = stored_layer['resolution_u']
                    layer_setting.resolution_u = stored_layer['resolution_u']
                    layer_setting.bevel_resolution = stored_layer['bevel_resolution']
                    layer_setting.enable_points_redistribution = stored_layer['enable_points_redistribution']
                    layer_setting.point_spacing = stored_layer['point_spacing']
                    layer_setting.straight_removal = stored_layer['straight_removal']
                    layer_setting.straight_edge_tolerance = stored_layer['straight_edge_tolerance']
                    layer_setting.extrusion_depth = stored_layer['extrusion_depth']
                    layer_setting.bevel_depth = stored_layer['bevel_depth']
                    layer_setting.use_curve_offset = stored_layer['use_curve_offset']
                    layer_setting.geometry_rotation = stored_layer['geometry_rotation']
                    layer_setting.geometry_rotation_last = stored_layer['geometry_rotation_last']
#                    layer_setting.show_materials_expanded = stored_layer['show_materials_expanded']
                    layer_setting.material_mode = stored_layer['material_mode']
                    layer_setting.use_ambient_occlusion = stored_layer['use_ambient_occlusion']
                    layer_setting.ao_samples = stored_layer['ao_samples']
                    layer_setting.ao_distance = stored_layer['ao_distance']
#                    layer_setting.show_ao_settings = stored_layer['show_ao_settings']
                    layer_setting.material_metallic = stored_layer['material_metallic']
                    layer_setting.material_roughness = stored_layer['material_roughness']
                    
                    # Also call the update after applying
                    layer_setting.update_material_properties(context)
                    
                    # Update geometry settings
                    try:
                        # Update the actual object (shape, curves, fill)
                        layer_setting.update_shape(context)
                        layer_setting.update_curve_settings(context)
                        layer_setting.update_fill_mode(context)
                    except Exception as e:
                        print(f"Error updating layer {layer_setting.layer_name}: {str(e)}")
                    
                    # Apply points redistribution if enabled
                    if layer_setting.enable_points_redistribution:
                        obj = bpy.data.objects.get(layer_setting.layer_name)
                        if obj and obj.type == 'CURVE':
                            resample_curve(
                                obj,
                                layer_setting.point_spacing,
                                layer_setting.straight_removal,
                                layer_setting.straight_edge_tolerance
                            )
                    
                    # NOW apply layer offset AFTER all other updates
                    layer_setting.layer_offset_percentage = stored_layer['layer_offset_percentage']
                    layer_setting.layer_z_position = stored_layer.get('layer_z_position', 0.0)
                    
                    # Finally update positions once at the end
                    layer_setting.update_layer_positions(context)
                    
                    break
    
    def complete_cleanup(self, context):
        """Completely clean up everything from Blender"""
        print("[CLEANUP DEBUG] Starting complete cleanup...")
        
        # Force object mode
        try:
            if context.mode != 'OBJECT':
                bpy.ops.object.mode_set(mode='OBJECT')
        except:
            pass
        
        # Delete all objects without selection
        for obj in list(bpy.data.objects):
            try:
                bpy.data.objects.remove(obj, do_unlink=True)
            except Exception as e:
                print(f"[CLEANUP WARNING] Could not remove {obj.name}: {e}")
        
        # Remove all collections except Scene Collection
        for collection in list(bpy.data.collections):
            if collection.name != "Scene Collection":
                try:
                    bpy.data.collections.remove(collection)
                except:
                    pass
        
        # Clean up orphan data
        for mesh in list(bpy.data.meshes):
            if mesh.users == 0:
                bpy.data.meshes.remove(mesh)
        
        for curve in list(bpy.data.curves):
            if curve.users == 0:
                bpy.data.curves.remove(curve)
        
        for material in list(bpy.data.materials):
            if material.users == 0 and not material.use_fake_user:
                bpy.data.materials.remove(material)
        
        for image in list(bpy.data.images):
            if image.users == 0:
                bpy.data.images.remove(image)
        
        # Clear layer settings
        context.scene.svg_layers_props.layer_settings.clear()
    
    def export_layers_as_glb(self, context, props, export_path):
        """Export all layers as a single GLB (reusing logic from ExportLayersGLBOperator)"""
        print(f"[BATCH DEBUG] Starting export for: {export_path.name}")
        
        from pathlib import Path
        full_export_path = Path(export_path)

        # Store MOF exe path in the scene so apply_uv_unwrap() can find it
        if getattr(self, "_mof_exe_path", None):
            context.scene['_temp_batch_mof_exe'] = self._mof_exe_path
            print(f"[BATCH DEBUG] Stored MOF exe in scene: {self._mof_exe_path}")
        else:
            print(f"[BATCH DEBUG] No MOF exe to store (single export or MOF not needed)")
        
        # Find all layer objects
        layer_objects = []
        for layer_setting in props.layer_settings:
            obj = bpy.data.objects.get(layer_setting.layer_name)
            if obj:
                layer_objects.append(obj)
        
        if not layer_objects:
            raise Exception("No layers found to export")
        
            print(f"[BATCH DEBUG] Found {len(layer_objects)} layers to export")
        
        # Store original visibility states
        original_states = []
        for obj in layer_objects:
            original_states.append({'obj': obj, 'hide_viewport': obj.hide_viewport, 'hide_render': obj.hide_render})
        
        mof_extract_path = None
        mof_exe_path = None
        mof_needed = any((s.uv_method == 'MOF' and s.use_ambient_occlusion) for s in props.layer_settings)
        if mof_needed:
            addon_dir = os.path.dirname(os.path.realpath(__file__))
            mof_zip_path = os.path.join(addon_dir, "resources", "MinistryOfFlat_Release.zip")
            if os.path.exists(mof_zip_path):
                try:
                    mof_extract_path = tempfile.mkdtemp(prefix="single_mof_")
                    with zipfile.ZipFile(mof_zip_path, 'r') as zip_ref:
                        zip_ref.extractall(mof_extract_path)
                    for root, dirs, files in os.walk(mof_extract_path):
                        for file in files:
                            if file.lower() == "unwrapconsole3.exe":
                                mof_exe_path = os.path.join(root, file)
                                break
                        if mof_exe_path:
                            break
                    if mof_exe_path:
                        context._single_mof_exe = mof_exe_path
                        context._single_mof_extract_path = mof_extract_path
                except Exception:
                    pass
        
        try:
            # Make all layers visible for duplication
            for obj in layer_objects:
                obj.hide_viewport = False
                obj.hide_render = False
            
            # Select all layer objects
            bpy.ops.object.select_all(action='DESELECT')
            for obj in layer_objects:
                obj.select_set(True)
            
            # Set Layer 1 as active if it exists
            layer_1 = None
            for obj in layer_objects:
                if obj.name == "Layer 1":
                    layer_1 = obj
                    break
            
            if layer_1:
                context.view_layer.objects.active = layer_1
            else:
                context.view_layer.objects.active = layer_objects[0]
            
            # Force depsgraph update BEFORE duplication
            context.view_layer.update()
            depsgraph = context.evaluated_depsgraph_get()
            depsgraph.update()
            
            # Duplicate all selected objects at once
            bpy.ops.object.duplicate_move(OBJECT_OT_duplicate={"linked":False})

            # Get the duplicated objects (they are now selected)
            temp_objects = context.selected_objects.copy()
            
            settings_by_name = {s.layer_name: s for s in props.layer_settings}
            
            # Process each object individually
            for obj in temp_objects:
                if obj.data.materials and obj.data.materials[0]:
                    if obj.data.materials[0].users > 1:
                        obj.data.materials[0] = obj.data.materials[0].copy()
                
                bpy.ops.object.select_all(action='DESELECT')
                obj.select_set(True)
                context.view_layer.objects.active = obj
                
                # Convert to mesh if it's a curve
                if obj.type == 'CURVE':
                    # Remove Edge Split modifier before conversion
                    for modifier in list(obj.modifiers):
                        if modifier.type == 'EDGE_SPLIT':
                            obj.modifiers.remove(modifier)
                    
                    # Convert to mesh
                    bpy.ops.object.convert(target='MESH')
                    print(f"[FIRST EXPORT CHECK] Converted {obj.name} to mesh")

                    # Apply transforms (first time)
                    bpy.ops.object.transform_apply(location=False, rotation=True, scale=True)
                    print(f"[FIRST EXPORT CHECK] Applied transforms (1st) for {obj.name}")

                    # Merge vertices by distance
                    merge_by_distance_bmesh(obj, threshold=0.000001)
                    print(f"[FIRST EXPORT CHECK] Merged vertices for {obj.name}")

                    # Reset normal vectors
                    if obj.data.has_custom_normals:
                        obj.data.normals_split_custom_set([])

                    # Update normals using the new API in Blender 4.5
                    obj.data.update()  # This updates the mesh including normals 
            
            # Select all temp objects
            bpy.ops.object.select_all(action='DESELECT')
            for obj in temp_objects:
                obj.select_set(True)
            
            # Set active to Layer 1 copy if exists
            layer_1_temp = None
            for obj in temp_objects:
                if "Layer 1" in obj.name:
                    layer_1_temp = obj
                    context.view_layer.objects.active = obj
                    break
            
            if not layer_1_temp and temp_objects:
                context.view_layer.objects.active = temp_objects[0]
            
            # Rotate everything 90 degrees around X axis
            for obj in temp_objects:
                original_loc = obj.location.copy()
                obj.rotation_euler[0] = math.radians(90)
                new_loc = obj.location.copy()
                new_loc.y = -original_loc.z
                new_loc.z = original_loc.y
                obj.location = new_loc
            
            # Apply UV unwrap and baking for EACH layer that has AO enabled
            # Check if cancelled
            if self._is_cancelled:
                # Clean up temp objects
                bpy.ops.object.select_all(action='DESELECT')
                for obj in temp_objects:
                    obj.select_set(True)
                bpy.ops.object.delete(use_global=False)
                return
            # First, hide all ORIGINAL layer objects to prevent shadow baking
            for layer_setting in props.layer_settings:
                original_obj = bpy.data.objects.get(layer_setting.layer_name)
                if original_obj:
                    original_obj.hide_set(True)
                    original_obj.hide_render = True

            for obj in temp_objects:
                # Check if cancelled (only for batch export)
                if hasattr(self, '_is_cancelled') and self._is_cancelled:
                    # Clean up temp objects and exit
                    bpy.ops.object.select_all(action='DESELECT')
                    for o in temp_objects:
                        o.select_set(True)
                    bpy.ops.object.delete(use_global=False)
                    return
                
                if obj.type == 'MESH':
                    # Find the layer settings for this specific object
                    base_name = obj.name.split('.')[0]
                    layer_setting = settings_by_name.get(base_name)
                    
                    # Only process if this layer has AO enabled
                    if layer_setting and layer_setting.use_ambient_occlusion:
                        # Apply UV unwrap with THIS layer's settings
                        bpy.ops.object.select_all(action='DESELECT')
                        obj.select_set(True)
                        context.view_layer.objects.active = obj
                        
                        # Clear any existing UV maps
                        bpy.ops.object.mode_set(mode='OBJECT')
                        while obj.data.uv_layers:
                            obj.data.uv_layers.remove(obj.data.uv_layers[0])
                        
                        # Back to edit mode for UV unwrap
                        bpy.ops.object.mode_set(mode='EDIT')
                        bpy.ops.mesh.select_all(action='SELECT')
                        bpy.ops.object.mode_set(mode='OBJECT')
                        
                        print(f"Applying UV unwrap ({layer_setting.uv_method}) to {obj.name} with settings from {layer_setting.layer_name}")
                        ok = apply_uv_unwrap(obj, layer_setting, context)
                        if not ok:
                            print(f"[UV WARN] Skipping bake for {obj.name} (unwrap failed)")
                            continue

                        # Only now add Edge Split and bake
                        edge_split = obj.modifiers.new(name="EdgeSplit", type='EDGE_SPLIT')
                        edge_split.split_angle = math.radians(30)

                        if obj.data.materials:
                            print(f"Baking textures for {obj.name} with settings from {layer_setting.layer_name}")
                            bake_textures_for_layer(obj, layer_setting, context)
            
            # Unhide original objects after baking
            for layer_setting in props.layer_settings:
                original_obj = bpy.data.objects.get(layer_setting.layer_name)
                if original_obj:
                    original_obj.hide_set(False)
                    original_obj.hide_render = False

            # Ensure all temp objects are selected for export
            bpy.ops.object.select_all(action='DESELECT')
            for obj in temp_objects:
                obj.select_set(True)
            
            # Export with default settings
            bpy.ops.export_scene.gltf(
                filepath=str(full_export_path),
                use_selection=True,
                export_format='GLB',
                export_apply=True
            )
            print(f"[WRITE DEBUG] Wrote GLB -> {full_export_path}")
            
            # Delete temporary objects
            bpy.ops.object.delete(use_global=False)
            
            print(f"[BATCH DEBUG] Export successful: {export_path.name}")
            self.report({'INFO'}, f"Exported to: {full_export_path.name}")
            
        except Exception as e:
            print(f"[BATCH ERROR] Export failed: {str(e)}")
            # Try to clean up any temp objects more safely
            try:
                # Ensure object mode
                if context.mode != 'OBJECT':
                    bpy.ops.object.mode_set(mode='OBJECT')
            except:
                pass
        
            # Manual cleanup without selection
            for obj in list(bpy.data.objects):
                if "_export_temp" in obj.name or ".001" in obj.name:
                    try:
                        bpy.data.objects.remove(obj, do_unlink=True)
                    except:
                        pass
        
            raise  # Re-raise the error
            
        finally:
            print(f"[CLEANUP DEBUG] Starting cleanup...")
            props = context.scene.svg_layers_props
            props.is_exporting = False
            
            # Clean up baked textures and materials
            for img in list(bpy.data.images):
                if "_BaseColor" in img.name or "_Metallic" in img.name or "_Roughness" in img.name or "_AO" in img.name:
                    bpy.data.images.remove(img)
                    
            # Clean up glTF Material Output node group
            if "glTF Material Output" in bpy.data.node_groups:
                try:
                    bpy.data.node_groups.remove(bpy.data.node_groups["glTF Material Output"])
                except:
                    pass
            
            # Restore original states
            for state in original_states:
                obj = state['obj']
                if obj.name in bpy.data.objects:
                    obj.hide_viewport = state['hide_viewport']
                    obj.hide_render = state['hide_render']
                    
            # Clean up orphan data
            for mesh in list(bpy.data.meshes):
                if mesh.users == 0:
                    bpy.data.meshes.remove(mesh)
            
            # Restore selection
            bpy.ops.object.select_all(action='DESELECT')
            for obj in layer_objects:
                if obj.name in bpy.data.objects:
                    obj.select_set(True)
            
            if layer_objects:
                context.view_layer.objects.active = layer_objects[0]
                
            # Clean up temp MOF exe reference
            if '_temp_batch_mof_exe' in context.scene:
                print(f"[CLEANUP DEBUG] Removing temp MOF exe from scene")
                del context.scene['_temp_batch_mof_exe']
                
            print(f"[CLEANUP DEBUG] Cleanup completed")
            
        return {'FINISHED'}
    
    def get_export_name_from_folder(self):
        """Get export name from the testing folder path"""
        props = bpy.context.scene.svg_layers_props
        if props.testing_folder_path:
            folder_path = Path(props.testing_folder_path)
            return folder_path.name
        return None
    
    def get_unique_filename(self, folder_path, base_name):
        """Generate unique filename with incrementing suffix if needed"""
        filename = f"{base_name}.glb"
        full_path = folder_path / filename
        
        if not full_path.exists():
            return filename
        
        counter = 2
        while True:
            filename = f"{base_name}_{counter:02d}.glb"
            full_path = folder_path / filename
            if not full_path.exists():
                return filename
            counter += 1

    def get_unique_output_folder(self, bundle_folder):
        """Generate unique output folder name with suffix if needed"""
        base_name = "Processed Models to GLB"
        output_folder = bundle_folder / base_name
        
        if not output_folder.exists():
            return output_folder
        
        # Folder exists, find unique name with suffix
        counter = 2
        while True:
            folder_name = f"{base_name}_{counter:02d}"
            output_folder = bundle_folder / folder_name
            if not output_folder.exists():
                return output_folder
            counter += 1

    def get_valid_model_folders(self, bundle_folder):
        """Get only folders that contain SVG files"""
        valid_folders = []
        all_folders = [f for f in bundle_folder.iterdir() if f.is_dir()]
        
        # Filter out the output folders
        all_folders = [f for f in all_folders if not f.name.startswith("Processed Models to GLB")]
        
        for folder in all_folders:
            svg_files = list(folder.glob("*.svg"))
            if svg_files:  # Only include if it has SVG files
                valid_folders.append(folder)
        
        return sorted(valid_folders)


class SavePresetOperator(Operator):
    """Save current layer settings as preset"""
    bl_idname = "svg_layers.save_preset"
    bl_label = "Save Preset"
    
    def invoke(self, context, event):
        props = context.scene.svg_layers_props
        # Add layer count to the default preset name
        layer_count = len(props.layer_settings)
        props.preset_name = f"My Preset ({layer_count})"
        return context.window_manager.invoke_props_dialog(self)
    
    def draw(self, context):
        props = context.scene.svg_layers_props
        layout = self.layout
        
        # Show current layer count info
        layer_count = len(props.layer_settings)
        layout.label(text=f"Saving {layer_count} layer(s)", icon='INFO')
        
        # Show the name field
        layout.prop(props, "preset_name")
    
    def execute(self, context):
        props = context.scene.svg_layers_props
        
        if not props.preset_name:
            self.report({'WARNING'}, "Please enter a preset name")
            return {'CANCELLED'}
        
        # Extract the base name (remove any existing layer count)
        import re
        base_name = re.sub(r'\s*\(\d+\)$', '', props.preset_name)
        
        # Save with just the base name (internally)
        preset_file = props.save_preset(base_name)
        props.current_preset = base_name
        
        layer_count = len(props.layer_settings)
        self.report({'INFO'}, f"Saved preset: {base_name} ({layer_count})")
        
        # Force refresh of enum
        for area in context.screen.areas:
            area.tag_redraw()
        
        return {'FINISHED'}

class DeletePresetOperator(Operator):
    """Delete selected preset"""
    bl_idname = "svg_layers.delete_preset"
    bl_label = "Delete Preset"
    
    def execute(self, context):
        props = context.scene.svg_layers_props
        
        if props.presets == 'NONE':
            self.report({'WARNING'}, "No preset selected")
            return {'CANCELLED'}
        
        if props.delete_preset(props.presets):
            self.report({'INFO'}, f"Deleted preset: {props.presets}")
            props.current_preset = ""
            props.presets = 'NONE'
            
            # Force UI redraw
            for area in context.screen.areas:
                area.tag_redraw()
        
        return {'FINISHED'}

class ExportPresetOperator(Operator, ExportHelper):
    """Export preset to file"""
    bl_idname = "svg_layers.export_preset"
    bl_label = "Export Preset"
    
    filename_ext = ".json"
    
    filter_glob: StringProperty(
        default="*.json",
        options={'HIDDEN'},
    )
    
    def execute(self, context):
        props = context.scene.svg_layers_props
        
        if props.presets == 'NONE':
            self.report({'WARNING'}, "No preset selected")
            return {'CANCELLED'}
        
        # Copy preset file to destination
        preset_dir = props.get_preset_directory()
        source = preset_dir / f"{props.presets}.json"
        
        if source.exists():
            import shutil
            shutil.copy2(source, self.filepath)
            self.report({'INFO'}, f"Exported preset to: {self.filepath}")
        
        return {'FINISHED'}
    
    def invoke(self, context, event):
        props = context.scene.svg_layers_props
        
        # Set default filename with layer count
        if props.presets != 'NONE':
            preset_dir = props.get_preset_directory()
            preset_file = preset_dir / f"{props.presets}.json"
            
            if preset_file.exists():
                try:
                    with open(preset_file, 'r') as f:
                        data = json.load(f)
                        layer_count = len(data.get('layers', []))
                        # Set the default filename with layer count
                        self.filepath = f"{props.presets} ({layer_count}).json"
                except:
                    self.filepath = f"{props.presets}.json"
        
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

class ImportPresetOperator(Operator):
    """Import preset from file"""
    bl_idname = "svg_layers.import_preset"
    bl_label = "Import Preset"
    
    filepath: StringProperty(
        name="File Path",
        subtype='FILE_PATH',
    )
    
    filter_glob: StringProperty(
        default="*.json",
        options={'HIDDEN'},
    )
    
    def execute(self, context):
        props = context.scene.svg_layers_props
        
        try:
            from pathlib import Path
            import shutil
            
            # Get preset name from filename
            preset_name = Path(self.filepath).stem
            
            # Copy to preset directory
            preset_dir = props.get_preset_directory()
            dest = preset_dir / f"{preset_name}.json"
            
            # Check if exists
            if dest.exists():
                preset_name = f"{preset_name}_imported"
                dest = preset_dir / f"{preset_name}.json"
            
            shutil.copy2(self.filepath, dest)
            
            self.report({'INFO'}, f"Imported preset: {preset_name}")
            
            # Force UI redraw
            for area in context.screen.areas:
                area.tag_redraw()
            
        except Exception as e:
            self.report({'ERROR'}, f"Failed to import preset: {str(e)}")
            return {'CANCELLED'}
        
        return {'FINISHED'}
    
    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}


class RemoveImportedModelOperator(Operator):
    """Remove all imported SVG layers and their data"""
    bl_idname = "svg_layers.remove_imported_model"
    bl_label = "Remove Imported Model"
    bl_description = "Remove all imported layers, collections, and materials"
    bl_options = {'UNDO'}
    
    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)
    
    def execute(self, context):
        props = context.scene.svg_layers_props
        
        # First, identify which collections contain our layer objects BEFORE removing them
        collections_to_remove = set()
        for layer_setting in props.layer_settings:
            obj = bpy.data.objects.get(layer_setting.layer_name)
            if obj:
                # Find all collections that contain this object
                for collection in bpy.data.collections:
                    if obj.name in collection.objects:
                        collections_to_remove.add(collection)
        
        # Collect materials that belong to THIS imported model only
        materials_to_remove = set()
        objects_to_remove = []
        
        # Collect materials from current layer objects ONLY
        for layer_setting in props.layer_settings:
            obj = bpy.data.objects.get(layer_setting.layer_name)
            if obj:
                objects_to_remove.append(obj)
                # Get materials directly assigned to this object
                if obj.data and hasattr(obj.data, 'materials'):
                    for mat in obj.data.materials:
                        if mat:
                            materials_to_remove.add(mat)
        
        # Remove all layer objects
        removed_count = 0
        for obj in objects_to_remove:
            obj_data = obj.data
            bpy.data.objects.remove(obj, do_unlink=True)
            
            # Remove the curve/mesh data if it has no users
            if obj_data and obj_data.users == 0:
                if obj_data.name in bpy.data.curves:
                    bpy.data.curves.remove(obj_data)
                elif obj_data.name in bpy.data.meshes:
                    bpy.data.meshes.remove(obj_data)
            
            removed_count += 1
        
        # Clear layer settings
        props.layer_settings.clear()
        
        # Now remove the collections we identified earlier
        for collection in collections_to_remove:
            if collection.name != "Scene Collection":
                # Only remove if it's now empty or was specifically created for these layers
                if len(collection.objects) == 0 or collection in collections_to_remove:
                    try:
                        bpy.data.collections.remove(collection)
                    except:
                        pass  # Collection might be linked elsewhere
        
        # Force remove the materials that were collected from the imported objects
        for mat in materials_to_remove:
            if mat and mat.name in bpy.data.materials:
                # Force removal with do_unlink=True
                bpy.data.materials.remove(mat, do_unlink=True)
        
        # Clean up orphan data
        for curve in list(bpy.data.curves):
            if curve.users == 0:
                bpy.data.curves.remove(curve)
        
        for mesh in list(bpy.data.meshes):
            if mesh.users == 0:
                bpy.data.meshes.remove(mesh)
        
        # Reset testing folder path since model is removed
        props.testing_folder_path = ""
        
        # Reset preset selection
        props.presets = 'NONE'
        props.current_preset = ""
        
        # Force UI redraw
        for area in context.screen.areas:
            if area.type == 'VIEW_3D':
                area.tag_redraw()
        
        self.report({'INFO'}, f"Removed {removed_count} layers and all associated materials")
        return {'FINISHED'}


class VIEW3D_PT_svg_layers_main(Panel):
    """Main panel for SVG to 3D Layers addon"""
    bl_label = "SVG to 3D - Layers"
    bl_idname = "VIEW3D_PT_svg_layers_main"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "SVG Layers"
    
    def draw(self, context):
        layout = self.layout
        props = context.scene.svg_layers_props
        
        # Check if processing is active - show progress at the top
        if props.is_processing:
            # Show processing status box at the top
            box = layout.box()
            box.alert = False
            col = box.column()
            
            # Title
            row = col.row()
            if props.progress_total > 0:
                row.label(text="BATCH EXPORT PROCESSING", icon='TIME')
            else:
                row.label(text="SINGLE EXPORT PROCESSING", icon='TIME')
            col.separator()
            
            # Progress bar
            if props.progress_total > 0:
                # Progress bar section
                progress_col = col.column()
                progress_col.scale_y = 2.0
                
                # Progress bar with file count as the label
                progress_col.prop(props, "progress_percentage", 
                                 text=f"{props.progress_current} of {props.progress_total} models",
                                 slider=True)
            
            # Current file info
            col.separator()
            if props.progress_filename:
                col.label(text=f"Processing: {props.progress_filename}", icon='FILE_FOLDER')
            
            # Cancel instruction - RED
            col.separator()
            row = col.row()
            row.scale_y = 1.5
            row.alert = True  # This makes it red
            row.label(text="Press ESC to Cancel", icon='CANCEL')
            
            layout.separator()
        
        # Testing Model Section
        box = layout.box()
        col = box.column(align=True)

        # Section header
        row = col.row()
        row.label(text="Testing Model", icon='EXPERIMENTAL')

        col.separator()

        # Folder selection
        col.label(text="Select Folder:")
        row = col.row(align=True)
        row.prop(props, "testing_folder_path", text="")
        if props.testing_folder_path:  # Only show X button if there's a path
            row.operator("svg_layers.clear_testing_folder", text="", icon='X')
        
        # Import button - always visible
        col.separator()
        row = col.row()
        row.scale_y = 1.3
        row.operator("svg_layers.import_testing_svgs", text="Import", icon='IMPORT')
        
        # Remove button - only show if there are actual imported layer objects
        has_imported_layers = False
        for layer_setting in props.layer_settings:
            if bpy.data.objects.get(layer_setting.layer_name):
                has_imported_layers = True
                break

        if has_imported_layers:
            col.separator(factor=0.5)
            row = col.row()
            row.scale_y = 1.0  # Slimmer than Import button
            row.operator("svg_layers.remove_imported_model", text="Remove Imported Model", icon='TRASH')
        
        # Presets Section
        layout.separator()
        box = layout.box()
        col = box.column(align=True)

        # Section header
        row = col.row()
        row.label(text="Presets", icon='PRESET')
        
        col = box.column()

        # Preset dropdown and buttons
        row = col.row(align=True)
        row.prop(props, "presets", text="")
        row.operator("svg_layers.save_preset", text="", icon='ADD')
        row.operator("svg_layers.delete_preset", text="", icon='REMOVE')
        row.separator()
        row.operator("svg_layers.export_preset", text="", icon='EXPORT')
        row.operator("svg_layers.import_preset", text="", icon='IMPORT')
        
        # Layers Section
        layout.separator()
        box = layout.box()
        col = box.column(align=True)

        # Section header with reset all button
        row = col.row()
        row.label(text="Layers", icon='SETTINGS')
        row.operator("svg_layers.reset_all_layers", text="", icon='LOOP_BACK')
        
        # Count valid layers (ones that have both settings AND objects)
        valid_layers = []
        for layer_setting in props.layer_settings:
            if bpy.data.objects.get(layer_setting.layer_name):
                valid_layers.append(layer_setting)

        # Show settings for each layer only if there are valid layers
        if valid_layers:
            # Sort layers by number and REVERSE for bottom-to-top display
            sorted_layers = sorted(valid_layers, 
                                 key=lambda x: int(x.layer_name.split()[-1]) if x.layer_name.split()[-1].isdigit() else 0,
                                 reverse=True)  # This reverses the order
            
            for layer_setting in sorted_layers:
                # Check if the layer object still exists
                obj = bpy.data.objects.get(layer_setting.layer_name)
                if obj:
                    col.separator()
                    
                    # Layer box
                    layer_box = col.box()
                    layer_col = layer_box.column(align=True)
                    
                    # Layer name - expandable
                    row = layer_col.row(align=True)
                    row.scale_y = 1.3
                    row.prop(layer_setting, "show_layer",
                            text=layer_setting.layer_name,
                            icon='TRIA_DOWN' if layer_setting.show_layer else 'TRIA_RIGHT',
                            emboss=True,
                            toggle=True)
                    # Add reset button for entire layer
                    op = row.operator("svg_layers.reset_layer_settings", text="", icon='LOOP_BACK')
                    op.layer_name = layer_setting.layer_name
                    
                    # Only show layer contents if expanded
                    if layer_setting.show_layer:
                        layer_col.separator(factor=0.5)
                    
                        # Geometry Settings (expandable)
                        layer_col.separator(factor=2.0)
                        row = layer_col.row(align=True)
                        row.prop(layer_setting, "show_expanded",
                                text="Geometry Settings",
                                icon='TRIA_DOWN' if layer_setting.show_expanded else 'TRIA_RIGHT',
                                emboss=True,
                                toggle=True)
                        # Add reset button for geometry section
                        op = row.operator("svg_layers.reset_geometry_settings", text="", icon='LOOP_BACK')
                        op.layer_name = layer_setting.layer_name

                        # Show Geometry settings only if expanded
                        if layer_setting.show_expanded:
                            subcol = layer_col.column(align=False)
                            subcol.separator(factor=0.5)
                            
                            # Add Auto-Adjust toggle at the top
                            row = subcol.row(align=True)
                            row.prop(layer_setting, "auto_adjust_layers", 
                                     icon='LINKED' if layer_setting.auto_adjust_layers else 'UNLINKED')
                            
                            # Add sync button
                            sub = row.row(align=True)
                            sub.scale_x = 1.0
                            sync_icon = 'UV_SYNC_SELECT' if props.sync_auto_adjust_layers else 'LOCKED'
                            op = sub.operator("svg_layers.toggle_sync", text="", icon=sync_icon, depress=props.sync_auto_adjust_layers)
                            op.sync_type = 'AUTO_ADJUST_LAYERS'
                            op.layer_name = layer_setting.layer_name
                            # Add reset button
                            op = row.operator("svg_layers.reset_property", text="", icon='LOOP_BACK')
                            op.layer_name = layer_setting.layer_name
                            op.property_name = "auto_adjust_layers"
                            
                            subcol.separator()
                            
                            # Extrusion Depth
                            row = subcol.row(align=True)
                            row.scale_y = 1.2
                            row.prop(layer_setting, "extrusion_depth")
                            op = row.operator("svg_layers.reset_property", text="", icon='LOOP_BACK')
                            op.layer_name = layer_setting.layer_name
                            op.property_name = "extrusion_depth"
                            
                            # Layer Offset OR Z Position (only for layers other than Layer 1)
                            if layer_setting.layer_name != "Layer 1":
                                row = subcol.row(align=True)
                                row.scale_y = 1.2
                                
                                # Find the previous layer to check its auto_adjust setting
                                prev_layer_auto_adjust = True  # Default to true
                                layer_number = int(layer_setting.layer_name.split()[-1]) if layer_setting.layer_name.split()[-1].isdigit() else 0
                                if layer_number > 1:
                                    prev_layer_name = f"Layer {layer_number - 1}"
                                    for other_setting in props.layer_settings:
                                        if other_setting.layer_name == prev_layer_name:
                                            prev_layer_auto_adjust = other_setting.auto_adjust_layers
                                            break
                                
                                if prev_layer_auto_adjust:
                                    # Previous layer allows auto-stacking, show Layer Offset
                                    # Calculate percentage for display
                                    if layer_setting.layer_offset_percentage >= 0:
                                        percentage_text = f"{abs(layer_setting.layer_offset_percentage):.0f}%"
                                    else:
                                        percentage_text = f"-{abs(layer_setting.layer_offset_percentage):.0f}%"
                                    
                                    row.prop(layer_setting, "layer_offset_distance_ui", 
                                            text=f"Layer Offset ({percentage_text})")
                                    
                                    # Add sync button
                                    sub = row.row(align=True)
                                    sub.scale_x = 1.0
                                    sync_icon = 'UV_SYNC_SELECT' if props.sync_layer_offset else 'LOCKED'
                                    op = sub.operator("svg_layers.toggle_sync", text="", icon=sync_icon, depress=props.sync_layer_offset)
                                    op.sync_type = 'LAYER_OFFSET'
                                    op.layer_name = layer_setting.layer_name
                                    
                                    # Reset button
                                    op = row.operator("svg_layers.reset_property", text="", icon='LOOP_BACK')
                                    op.layer_name = layer_setting.layer_name
                                    op.property_name = "layer_offset_percentage"
                                else:
                                    # Previous layer doesn't allow auto-stacking, show Z Position
                                    row.prop(layer_setting, "layer_z_position", text="Z Position")
                                    
                                    # Add sync button for Z Position
                                    sub = row.row(align=True)
                                    sub.scale_x = 1.0
                                    sync_icon = 'UV_SYNC_SELECT' if props.sync_z_position else 'LOCKED'
                                    op = sub.operator("svg_layers.toggle_sync", text="", icon=sync_icon, depress=props.sync_z_position)
                                    op.sync_type = 'Z_POSITION'
                                    op.layer_name = layer_setting.layer_name
                                    
                                    # Reset button
                                    op = row.operator("svg_layers.reset_property", text="", icon='LOOP_BACK')
                                    op.layer_name = layer_setting.layer_name
                                    op.property_name = "layer_z_position"
                            
                            # Bevel Size
                            row = subcol.row(align=True)
                            row.scale_y = 1.2
                            # Get the actual bevel depth from the curve object
                            obj = bpy.data.objects.get(layer_setting.layer_name)
                            if obj and obj.type == 'CURVE':
                                bevel_depth = obj.data.bevel_depth
                                
                                # Calculate percentage for display ONLY
                                if layer_setting.extrusion_depth > 0:
                                    max_allowed_bevel = layer_setting.extrusion_depth * 0.5
                                    if max_allowed_bevel > 0:
                                        percentage = (bevel_depth / max_allowed_bevel) * 100
                                        percentage_text = f"{percentage:.1f}%"
                                    else:
                                        percentage_text = "0%"
                                else:
                                    percentage_text = "0%"
                                
                                # Show with percentage in brackets
                                row.prop(layer_setting, "bevel_depth", text=f"Bevel Depth ({percentage_text})")
                                
                                # Add sync button
                                sub = row.row(align=True)
                                sub.scale_x = 1.0
                                sync_icon = 'UV_SYNC_SELECT' if props.sync_bevel_depth else 'LOCKED'
                                op = sub.operator("svg_layers.toggle_sync", text="", icon=sync_icon, depress=props.sync_bevel_depth)
                                op.sync_type = 'BEVEL_DEPTH'
                                op.layer_name = layer_setting.layer_name
                                
                                # Reset button (only once)
                                op = row.operator("svg_layers.reset_property", text="", icon='LOOP_BACK')
                                op.layer_name = layer_setting.layer_name
                                op.property_name = "bevel_depth"
                            else:
                                row.prop(layer_setting, "bevel_depth", text="Bevel Depth (0%)")
                                # Reset button for when object doesn't exist
                                op = row.operator("svg_layers.reset_property", text="", icon='LOOP_BACK')
                                op.layer_name = layer_setting.layer_name
                                op.property_name = "bevel_depth"

                            # Curve Offset checkbox
                            row = subcol.row(align=True)
                            row.scale_y = 1.2
                            row.prop(layer_setting, "use_curve_offset")
                            # Add sync button
                            sub = row.row(align=True)
                            sub.scale_x = 1.0
                            sync_icon = 'UV_SYNC_SELECT' if props.sync_geometry_offset else 'LOCKED'
                            op = sub.operator("svg_layers.toggle_sync", text="", icon=sync_icon, depress=props.sync_geometry_offset)
                            op.sync_type = 'GEOMETRY_OFFSET'
                            op.layer_name = layer_setting.layer_name
                            op = row.operator("svg_layers.reset_property", text="", icon='LOOP_BACK')
                            op.layer_name = layer_setting.layer_name
                            op.property_name = "use_curve_offset"

                            # Geometry Rotation
                            row = subcol.row(align=True)
                            row.scale_y = 1.2
                            row.prop(layer_setting, "geometry_rotation", text="Geometry Rotation (Â°)")
                            # Add sync button
                            sub = row.row(align=True)
                            sub.scale_x = 1.0
                            sync_icon = 'UV_SYNC_SELECT' if props.sync_geometry_rotation else 'LOCKED'
                            op = sub.operator("svg_layers.toggle_sync", text="", icon=sync_icon, depress=props.sync_geometry_rotation)
                            op.sync_type = 'GEOMETRY_ROTATION'
                            op.layer_name = layer_setting.layer_name
                            op = row.operator("svg_layers.reset_property", text="", icon='LOOP_BACK')
                            op.layer_name = layer_setting.layer_name
                            op.property_name = "geometry_rotation"
                        
                            # Fill Mode
                            subcol.separator()
                            row = subcol.row(align=True)
                            row.label(text="Fill Mode:")
                            # Add sync button
                            sub = row.row(align=True)
                            sub.scale_x = 1.0
                            sync_icon = 'UV_SYNC_SELECT' if props.sync_fill_mode else 'LOCKED'
                            op = sub.operator("svg_layers.toggle_sync", text="", icon=sync_icon, depress=props.sync_fill_mode)
                            op.sync_type = 'FILL_MODE'
                            op.layer_name = layer_setting.layer_name
                            # Reset button
                            op = row.operator("svg_layers.reset_property", text="", icon='LOOP_BACK')
                            op.layer_name = layer_setting.layer_name
                            op.property_name = "fill_mode"

                            # Fill Mode buttons on the next row
                            row = subcol.row(align=True)
                            row.scale_y = 1.2
                            row.prop_enum(layer_setting, "fill_mode", 'NONE')
                            row.prop_enum(layer_setting, "fill_mode", 'BACK')
                            row.prop_enum(layer_setting, "fill_mode", 'FRONT')
                            row.prop_enum(layer_setting, "fill_mode", 'BOTH')
                        
                        # Curve Resolution (expandable)
                        layer_col.separator(factor=1.5)
                        row = layer_col.row(align=True)
                        row.prop(
                            layer_setting,
                            "show_curve_expanded",
                            text="Curve Resolution",
                            icon='TRIA_DOWN' if layer_setting.show_curve_expanded else 'TRIA_RIGHT',
                            emboss=True,
                            toggle=True
                        )
                        # Add reset button for curve section
                        op = row.operator("svg_layers.reset_curve_settings", text="", icon='LOOP_BACK')
                        op.layer_name = layer_setting.layer_name

                        if layer_setting.show_curve_expanded:
                            subcol = layer_col.column(align=False)
                            subcol.separator(factor=0.5)

                            # Resolution U with sync
                            r = subcol.row(align=True)
                            r.scale_y = 1.2
                            r.prop(layer_setting, "resolution_u")
                            # Add sync button
                            sub = r.row(align=True)
                            sub.scale_x = 1.0
                            sync_icon = 'UV_SYNC_SELECT' if props.sync_resolution_u else 'LOCKED'
                            op = sub.operator("svg_layers.toggle_sync", text="", icon=sync_icon, depress=props.sync_resolution_u)
                            op.sync_type = 'RESOLUTION_U'
                            op.layer_name = layer_setting.layer_name
                            op = r.operator("svg_layers.reset_property", text="", icon='LOOP_BACK')
                            op.layer_name = layer_setting.layer_name
                            op.property_name = "resolution_u"

                            # Bevel Resolution with sync
                            r = subcol.row(align=True)
                            r.scale_y = 1.2
                            r.prop(layer_setting, "bevel_resolution")
                            # Add sync button
                            sub = r.row(align=True)
                            sub.scale_x = 1.0
                            sync_icon = 'UV_SYNC_SELECT' if props.sync_bevel_resolution else 'LOCKED'
                            op = sub.operator("svg_layers.toggle_sync", text="", icon=sync_icon, depress=props.sync_bevel_resolution)
                            op.sync_type = 'BEVEL_RESOLUTION'
                            op.layer_name = layer_setting.layer_name
                            op = r.operator("svg_layers.reset_property", text="", icon='LOOP_BACK')
                            op.layer_name = layer_setting.layer_name
                            op.property_name = "bevel_resolution"

                            # Points Redistribution Section
                            subcol.separator()
                            r = subcol.row(align=True)
                            r.scale_y = 1.2
                            r.prop(layer_setting, "enable_points_redistribution")
                            op = r.operator("svg_layers.reset_points_redistribution", text="", icon='LOOP_BACK')
                            op.layer_name = layer_setting.layer_name

                            # Show settings and apply button when enabled
                            if layer_setting.enable_points_redistribution:
                                redistrib_box = subcol.box()
                                redistrib_col = redistrib_box.column(align=False)
                                
                                # Point Spacing with reset
                                r = redistrib_col.row(align=True)
                                r.scale_y = 1.0
                                r.prop(layer_setting, "point_spacing")
                                op = r.operator("svg_layers.reset_property", text="", icon='LOOP_BACK')
                                op.layer_name = layer_setting.layer_name
                                op.property_name = "point_spacing"

                                # Straight Removal with reset
                                r = redistrib_col.row(align=True)
                                r.scale_y = 1.0
                                r.prop(layer_setting, "straight_removal", slider=True)
                                op = r.operator("svg_layers.reset_property", text="", icon='LOOP_BACK')
                                op.layer_name = layer_setting.layer_name
                                op.property_name = "straight_removal"
                                
                                # Straight Edge Tolerance with reset
                                r = redistrib_col.row(align=True)
                                r.scale_y = 1.0
                                r.prop(layer_setting, "straight_edge_tolerance")
                                op = r.operator("svg_layers.reset_property", text="", icon='LOOP_BACK')
                                op.layer_name = layer_setting.layer_name
                                op.property_name = "straight_edge_tolerance"
                                
                                redistrib_col.separator(factor=0.5)
                                
                                # Apply button (no reset needed here)
                                r = redistrib_col.row()
                                r.scale_y = 1.3
                                op = r.operator("svg_layers.apply_points_redistribution", 
                                              text="Apply Redistribution", 
                                              icon='CHECKMARK')
                                op.layer_name = layer_setting.layer_name
                                
                        # Materials Settings
                        layer_col.separator(factor=3.0)  # Bigger spacing as requested

                        # Materials header with reset button
                        row = layer_col.row(align=True)
                        row.prop(layer_setting, "show_materials_expanded",
                                text="Materials",
                                icon='TRIA_DOWN' if layer_setting.show_materials_expanded else 'TRIA_RIGHT',
                                emboss=True,
                                toggle=True)
                        op = row.operator("svg_layers.reset_materials_settings", text="", icon='LOOP_BACK')
                        op.layer_name = layer_setting.layer_name

                        # Show materials settings only if expanded
                        if layer_setting.show_materials_expanded:
                            subcol = layer_col.column(align=False)
                            subcol.separator(factor=0.5)
                            
                            # Mode toggle buttons with reset and sync
                            row = subcol.row(align=True)
                            row.scale_y = 1.3
                            row.prop_enum(layer_setting, "material_mode", 'PRESERVED')
                            row.prop_enum(layer_setting, "material_mode", 'CUSTOM')
                            # Add sync button
                            sub = row.row(align=True)
                            sub.scale_x = 1.0
                            sync_icon = 'UV_SYNC_SELECT' if props.sync_material_mode else 'LOCKED'
                            op = sub.operator("svg_layers.toggle_material_mode_sync", text="", icon=sync_icon, depress=props.sync_material_mode)
                            op.layer_name = layer_setting.layer_name
                            # Add reset button
                            op = row.operator("svg_layers.reset_material_mode", text="", icon='LOOP_BACK')
                            op.layer_name = layer_setting.layer_name
                            
                            subcol.separator()
                            
                            # Show different content based on mode
                            if layer_setting.material_mode == 'PRESERVED':
                                # Ambient Occlusion checkbox with reset for the whole AO section
                                row = subcol.row(align=True)
                                row.scale_y = 1.2
                                row.prop(layer_setting, "use_ambient_occlusion")
                                if layer_setting.use_ambient_occlusion:
                                    row.prop(layer_setting, "show_ao_settings",
                                            text="",
                                            icon='TRIA_DOWN' if layer_setting.show_ao_settings else 'TRIA_RIGHT',
                                            emboss=False)
                                # Sync button
                                sub = row.row(align=True)
                                sub.scale_x = 1.0
                                sync_icon = 'UV_SYNC_SELECT' if props.sync_ambient_occlusion else 'LOCKED'
                                op = sub.operator("svg_layers.toggle_sync", text="", icon=sync_icon, depress=props.sync_ambient_occlusion)
                                op.sync_type = 'AMBIENT_OCCLUSION'
                                op.layer_name = layer_setting.layer_name
                                # Reset button for entire AO section
                                op = row.operator("svg_layers.reset_ambient_occlusion", text="", icon='LOOP_BACK')
                                op.layer_name = layer_setting.layer_name
                                
                                # Show AO-specific settings when enabled AND expanded
                                if layer_setting.use_ambient_occlusion and layer_setting.show_ao_settings:
                                    # Samples with reset
                                    row = subcol.row(align=True)
                                    row.scale_y = 1.0
                                    row.prop(layer_setting, "ao_samples", text="Samples")
                                    op = row.operator("svg_layers.reset_property", text="", icon='LOOP_BACK')
                                    op.layer_name = layer_setting.layer_name
                                    op.property_name = "ao_samples"
                                    
                                    # Distance with reset
                                    row = subcol.row(align=True)
                                    row.scale_y = 1.0
                                    row.prop(layer_setting, "ao_distance", text="Distance")
                                    op = row.operator("svg_layers.reset_property", text="", icon='LOOP_BACK')
                                    op.layer_name = layer_setting.layer_name
                                    op.property_name = "ao_distance"
                                    
                                    subcol.separator()

                                # Add label for Principled BSDF with sync button
                                subcol.separator()
                                # Principled BSDF with reset for the whole section
                                row = subcol.row(align=True)
                                row.label(text="Principled BSDF:")
                                # Sync button
                                sub = row.row(align=True)
                                sub.scale_x = 1.0
                                sync_icon = 'UV_SYNC_SELECT' if props.sync_principled_bsdf else 'LOCKED'
                                op = sub.operator("svg_layers.toggle_sync", text="", icon=sync_icon, depress=props.sync_principled_bsdf)
                                op.sync_type = 'PRINCIPLED_BSDF'
                                op.layer_name = layer_setting.layer_name
                                # Reset button for Principled BSDF section
                                op = row.operator("svg_layers.reset_principled_bsdf", text="", icon='LOOP_BACK')
                                op.layer_name = layer_setting.layer_name

                                # Base Color (no sync, no reset - just the color)
                                row = subcol.row(align=True)
                                row.scale_y = 1.0
                                row.prop(layer_setting, "material_base_color", text="")
                                subcol.separator()

                                # Metallic slider with reset
                                row = subcol.row(align=True)
                                row.scale_y = 1.0
                                row.prop(layer_setting, "material_metallic", slider=True)
                                op = row.operator("svg_layers.reset_property", text="", icon='LOOP_BACK')
                                op.layer_name = layer_setting.layer_name
                                op.property_name = "material_metallic"
                                
                                # Roughness slider with reset
                                row = subcol.row(align=True)
                                row.scale_y = 1.0
                                row.prop(layer_setting, "material_roughness", slider=True)
                                op = row.operator("svg_layers.reset_property", text="", icon='LOOP_BACK')
                                op.layer_name = layer_setting.layer_name
                                op.property_name = "material_roughness"
                                
                                # UV Unwrap section with sync button
                                subcol.separator()
                                row = subcol.row(align=True)
                                row.scale_y = 1.0
                                
                                # Gray out if AO is not enabled
                                row.enabled = layer_setting.use_ambient_occlusion
                                
                                row.prop(layer_setting, "show_uv_settings",
                                        icon='TRIA_DOWN' if layer_setting.show_uv_settings else 'TRIA_RIGHT',
                                        emboss=True)
                                # Add sync button for UV
                                sub = row.row(align=True)
                                sub.scale_x = 1.0
                                sync_icon = 'UV_SYNC_SELECT' if props.sync_uv_unwrap else 'LOCKED'
                                op = sub.operator("svg_layers.toggle_sync", text="", icon=sync_icon, depress=props.sync_uv_unwrap)
                                op.sync_type = 'UV_UNWRAP'
                                op.layer_name = layer_setting.layer_name
                                # This should call reset_uv_unwrap, not reset_property
                                op = row.operator("svg_layers.reset_uv_unwrap", text="", icon='LOOP_BACK')
                                op.layer_name = layer_setting.layer_name
                                
                                # Show UV settings if expanded
                                if layer_setting.show_uv_settings and layer_setting.use_ambient_occlusion:
                                    uv_box = subcol.box()
                                    uv_col = uv_box.column(align=False)
                                    
                                    # UV Method dropdown
                                    row = uv_col.row(align=True)
                                    row.prop(layer_setting, "uv_method", text="")
                                    op = row.operator("svg_layers.reset_property", text="", icon='LOOP_BACK')
                                    op.layer_name = layer_setting.layer_name
                                    op.property_name = "uv_method"
                                    
                                    uv_col.separator(factor=0.5)
                                    
                                    # Show settings based on selected method
                                    if layer_setting.uv_method == 'SMART':
                                        # Smart UV Project settings
                                        # Angle Limit
                                        row = uv_col.row(align=True)
                                        row.prop(layer_setting, "uv_angle_limit")
                                        op = row.operator("svg_layers.reset_property", text="", icon='LOOP_BACK')
                                        op.layer_name = layer_setting.layer_name
                                        op.property_name = "uv_angle_limit"
                                        
                                        # Margin Method
                                        row = uv_col.row(align=True)
                                        row.prop(layer_setting, "uv_margin_method", text="")
                                        op = row.operator("svg_layers.reset_property", text="", icon='LOOP_BACK')
                                        op.layer_name = layer_setting.layer_name
                                        op.property_name = "uv_margin_method"
                                        
                                        # Rotation Method  
                                        row = uv_col.row(align=True)
                                        row.prop(layer_setting, "uv_rotate_method", text="")
                                        op = row.operator("svg_layers.reset_property", text="", icon='LOOP_BACK')
                                        op.layer_name = layer_setting.layer_name
                                        op.property_name = "uv_rotate_method"
                                        
                                        # Island Margin
                                        row = uv_col.row(align=True)
                                        row.prop(layer_setting, "uv_island_margin")
                                        op = row.operator("svg_layers.reset_property", text="", icon='LOOP_BACK')
                                        op.layer_name = layer_setting.layer_name
                                        op.property_name = "uv_island_margin"
                                        
                                        # Area Weight
                                        row = uv_col.row(align=True)
                                        row.prop(layer_setting, "uv_area_weight")
                                        op = row.operator("svg_layers.reset_property", text="", icon='LOOP_BACK')
                                        op.layer_name = layer_setting.layer_name
                                        op.property_name = "uv_area_weight"
                                        
                                        # Checkboxes
                                        row = uv_col.row(align=True)
                                        row.prop(layer_setting, "uv_correct_aspect")
                                        op = row.operator("svg_layers.reset_property", text="", icon='LOOP_BACK')
                                        op.layer_name = layer_setting.layer_name
                                        op.property_name = "uv_correct_aspect"

                                        row = uv_col.row(align=True)
                                        row.prop(layer_setting, "uv_scale_to_bounds")
                                        op = row.operator("svg_layers.reset_property", text="", icon='LOOP_BACK')
                                        op.layer_name = layer_setting.layer_name
                                        op.property_name = "uv_scale_to_bounds"
                                    
                                    elif layer_setting.uv_method == 'CUBE':
                                        # Cube Projection settings
                                        # Cube Size (display only, auto-calculated)
                                        row = uv_col.row(align=True)
                                        row.prop(layer_setting, "cube_size")
                                        row.enabled = False  # Grayed out since it's auto-calculated
                                        op = row.operator("svg_layers.reset_property", text="", icon='LOOP_BACK')
                                        op.layer_name = layer_setting.layer_name
                                        op.property_name = "cube_size"
                                        
                                        # Correct Aspect
                                        row = uv_col.row(align=True)
                                        row.prop(layer_setting, "cube_correct_aspect")
                                        op = row.operator("svg_layers.reset_property", text="", icon='LOOP_BACK')
                                        op.layer_name = layer_setting.layer_name
                                        op.property_name = "cube_correct_aspect"
                                        
                                        # Clip to Bounds
                                        row = uv_col.row(align=True)
                                        row.prop(layer_setting, "cube_clip_to_bounds")
                                        op = row.operator("svg_layers.reset_property", text="", icon='LOOP_BACK')
                                        op.layer_name = layer_setting.layer_name
                                        op.property_name = "cube_clip_to_bounds"
                                        
                                        # Scale to Bounds
                                        row = uv_col.row(align=True)
                                        row.prop(layer_setting, "cube_scale_to_bounds")
                                        op = row.operator("svg_layers.reset_property", text="", icon='LOOP_BACK')
                                        op.layer_name = layer_setting.layer_name
                                        op.property_name = "cube_scale_to_bounds"
                                        
                                        row.prop(layer_setting, "cube_scale_to_bounds")
                                        op = row.operator("svg_layers.reset_property", text="", icon='LOOP_BACK')
                                        op.layer_name = layer_setting.layer_name
                                        op.property_name = "cube_scale_to_bounds"
                                    
                                    elif layer_setting.uv_method == 'MOF':
                                        # Check if MOF file exists
                                        addon_dir = os.path.dirname(os.path.realpath(__file__))
                                        mof_zip_path = os.path.join(addon_dir, "resources", "MinistryOfFlat_Release.zip")
                                        
                                        if not os.path.exists(mof_zip_path):
                                            error_box = uv_col.box()
                                            error_col = error_box.column()
                                            error_col.alert = True
                                            error_col.label(text="MOF file missing!", icon='ERROR')
                                            error_col.label(text="Place MinistryOfFlat_Release.zip in:")
                                            error_col.label(text="addon/resources/ folder")
                                        else:
                                            # MOF settings (without individual reset buttons)
                                            uv_col.label(text="MOF General Settings:")

                                            uv_col.prop(layer_setting, "mof_separate_hard_edges")
                                            uv_col.prop(layer_setting, "mof_separate_marked_edges")
                                            uv_col.prop(layer_setting, "mof_overlap_identical")
                                            uv_col.prop(layer_setting, "mof_overlap_mirrored")
                                            uv_col.prop(layer_setting, "mof_world_scale")
                                            uv_col.prop(layer_setting, "mof_use_normals")
                                            uv_col.prop(layer_setting, "mof_suppress_validation")
                                            uv_col.prop(layer_setting, "mof_smooth")
                                            uv_col.prop(layer_setting, "mof_keep_original")
                                            uv_col.prop(layer_setting, "mof_triangulate")
                                        
                                    # Packing checkbox with expand arrow and reset
                                    uv_col.separator()
                                    row = uv_col.row(align=True)
                                    row.prop(layer_setting, "enable_uv_packing")
                                    if layer_setting.enable_uv_packing:
                                        row.prop(layer_setting, "show_packing_settings",
                                                text="",
                                                icon='TRIA_DOWN' if layer_setting.show_packing_settings else 'TRIA_RIGHT',
                                                emboss=False)
                                    # Reset button for entire packing section
                                    op = row.operator("svg_layers.reset_packing", text="", icon='LOOP_BACK')
                                    op.layer_name = layer_setting.layer_name

                                    # Show packing settings if enabled and expanded
                                    if layer_setting.enable_uv_packing and layer_setting.show_packing_settings:
                                        # Use the same uv_col, no new box
                                        uv_col.separator(factor=0.5)
                                        
                                        # Shape Method
                                        row = uv_col.row(align=True)
                                        row.prop(layer_setting, "pack_shape_method", text="")
                                        op = row.operator("svg_layers.reset_property", text="", icon='LOOP_BACK')
                                        op.layer_name = layer_setting.layer_name
                                        op.property_name = "pack_shape_method"
                                        
                                        # Scale checkbox
                                        row = uv_col.row(align=True)
                                        row.prop(layer_setting, "pack_scale")
                                        op = row.operator("svg_layers.reset_property", text="", icon='LOOP_BACK')
                                        op.layer_name = layer_setting.layer_name
                                        op.property_name = "pack_scale"

                                        # Rotate checkbox
                                        row = uv_col.row(align=True)
                                        row.prop(layer_setting, "pack_rotate")
                                        op = row.operator("svg_layers.reset_property", text="", icon='LOOP_BACK')
                                        op.layer_name = layer_setting.layer_name
                                        op.property_name = "pack_rotate"
                                        
                                        # Rotation Method (only if rotate is enabled)
#                                        if layer_setting.pack_rotate:
#                                            row = uv_col.row(align=True)
#                                            row.prop(layer_setting, "pack_rotation_method", text="")
#                                            op = row.operator("svg_layers.reset_property", text="", icon='LOOP_BACK')
#                                            op.layer_name = layer_setting.layer_name
#                                            op.property_name = "pack_rotation_method"
                                        
                                        # Margin Method
                                        row = uv_col.row(align=True)
                                        row.prop(layer_setting, "pack_margin_method", text="")
                                        op = row.operator("svg_layers.reset_property", text="", icon='LOOP_BACK')
                                        op.layer_name = layer_setting.layer_name
                                        op.property_name = "pack_margin_method"
                                        
                                        # Margin value
                                        row = uv_col.row(align=True)
                                        row.prop(layer_setting, "pack_margin")
                                        op = row.operator("svg_layers.reset_property", text="", icon='LOOP_BACK')
                                        op.layer_name = layer_setting.layer_name
                                        op.property_name = "pack_margin"
                                        
                                        # Lock Pinned Islands
                                        row = uv_col.row(align=True)
                                        row.prop(layer_setting, "pack_pin_islands")
                                        op = row.operator("svg_layers.reset_property", text="", icon='LOOP_BACK')
                                        op.layer_name = layer_setting.layer_name
                                        op.property_name = "pack_pin_islands"
                                        
                                        # Lock Method (only if pin is enabled)
                                        if layer_setting.pack_pin_islands:
                                            row = uv_col.row(align=True)
                                            row.prop(layer_setting, "pack_pin_method", text="")
                                            op = row.operator("svg_layers.reset_property", text="", icon='LOOP_BACK')
                                            op.layer_name = layer_setting.layer_name
                                            op.property_name = "pack_pin_method"
                                        
                                        # Merge Overlapping
                                        row = uv_col.row(align=True)
                                        row.prop(layer_setting, "pack_merge_overlapping")
                                        op = row.operator("svg_layers.reset_property", text="", icon='LOOP_BACK')
                                        op.layer_name = layer_setting.layer_name
                                        op.property_name = "pack_merge_overlapping"
                                        
                                        # Pack to
                                        row = uv_col.row(align=True)
                                        row.prop(layer_setting, "pack_udim_source", text="")
                                        op = row.operator("svg_layers.reset_property", text="", icon='LOOP_BACK')
                                        op.layer_name = layer_setting.layer_name
                                        op.property_name = "pack_udim_source"
                                
                                # Baking section with sync button
                                subcol.separator()
                                row = subcol.row(align=True)
                                row.scale_y = 1.0
                                
                                # Gray out if AO is not enabled
                                row.enabled = layer_setting.use_ambient_occlusion
                                
                                row.prop(layer_setting, "show_baking_settings",
                                        icon='TRIA_DOWN' if layer_setting.show_baking_settings else 'TRIA_RIGHT',
                                        emboss=True)
                                # Add sync button for Baking
                                sub = row.row(align=True)
                                sub.scale_x = 1.0
                                sync_icon = 'UV_SYNC_SELECT' if props.sync_baking else 'LOCKED'
                                op = sub.operator("svg_layers.toggle_sync", text="", icon=sync_icon, depress=props.sync_baking)
                                op.sync_type = 'BAKING'
                                op.layer_name = layer_setting.layer_name
                                op = row.operator("svg_layers.reset_baking", text="", icon='LOOP_BACK')
                                op.layer_name = layer_setting.layer_name
                                
                                # Show Baking settings if expanded (rest stays the same)
                                if layer_setting.show_baking_settings and layer_setting.use_ambient_occlusion:
                                    bake_box = subcol.box()
                                    bake_col = bake_box.column(align=False)
                                    
                                    # Bake Method dropdown
                                    row = bake_col.row(align=True)
                                    row.prop(layer_setting, "bake_method", text="")
                                    
                                    bake_col.separator(factor=0.5)
                                    
                                    # Bake Settings label
                                    bake_col.label(text="Bake Settings:")
                                    
                                    # Samples
                                    row = bake_col.row(align=True)
                                    row.prop(layer_setting, "bake_samples")
                                    op = row.operator("svg_layers.reset_property", text="", icon='LOOP_BACK')
                                    op.layer_name = layer_setting.layer_name
                                    op.property_name = "bake_samples"
                                    
                                    # Margin
                                    row = bake_col.row(align=True)
                                    row.prop(layer_setting, "bake_margin")
                                    op = row.operator("svg_layers.reset_property", text="", icon='LOOP_BACK')
                                    op.layer_name = layer_setting.layer_name
                                    op.property_name = "bake_margin"
                                    
                                    bake_col.separator()
                                    
                                    # Texture Settings label
                                    bake_col.label(text="Texture Settings:")
                                    
                                    # Texture Resolution
                                    row = bake_col.row(align=True)
                                    row.prop(layer_setting, "texture_resolution")
                                    op = row.operator("svg_layers.reset_property", text="", icon='LOOP_BACK')
                                    op.layer_name = layer_setting.layer_name
                                    op.property_name = "texture_resolution"

                            elif layer_setting.material_mode == 'CUSTOM':
                                # Custom mode - empty for now
                                subcol.label(text="Custom settings coming soon", icon='INFO')  
                                
        else:
            col.separator()
            col.label(text="No layers imported yet", icon='INFO')
        
        # Export Section
        layout.separator()
        box = layout.box()
        col = box.column(align=True)

        # Section header
        row = col.row()
        row.label(text="Export", icon='EXPORT')

        col.separator()

        # Mode toggle buttons
        row = col.row(align=True)
        row.scale_y = 1.3
        row.prop_enum(props, "export_mode", 'SINGLE')
        row.prop_enum(props, "export_mode", 'BATCH')

        col.separator()

        # Show different UI based on mode
        if props.export_mode == 'SINGLE':
            # Folder selection
            col.label(text="Export Location:")
            row = col.row(align=True)
            row.prop(props, "export_folder_path", text="")
            row.operator("svg_layers.select_export_file", text="", icon='FILE_FOLDER')
            if props.export_folder_path:
                row.operator("svg_layers.clear_export_folder", text="", icon='X')
            
            # Export button
            col.separator()
            row = col.row()
            row.scale_y = 1.3
            row.operator("svg_layers.export_glb", text="Export", icon='EXPORT')
            
        else:  # BATCH mode
            # Show current folder info
            if props.testing_folder_path:
                current_folder = Path(props.testing_folder_path)
                bundle_folder = current_folder.parent
                
                col.label(text="Bundle Folder:", icon='FILE_FOLDER')
                col.label(text=f"  {bundle_folder.name}")
                
                # Count model folders with SVG files
                model_folders = [f for f in bundle_folder.iterdir() if f.is_dir() 
                                 and not f.name.startswith("Processed Models to GLB")]
                valid_folders = [f for f in model_folders if list(f.glob("*.svg"))]
                col.label(text=f"  {len(valid_folders)} model folders with SVGs found")
                if len(model_folders) > len(valid_folders):
                    col.label(text=f"  ({len(model_folders) - len(valid_folders)} empty folders will be skipped)")
            else:
                col.label(text="No folder selected", icon='ERROR')
            
            # Export button
            col.separator()
            row = col.row()
            row.scale_y = 1.3
            if props.testing_folder_path:
                row.operator("svg_layers.export_batch_glb", text="Export All Models", icon='EXPORT')
            else:
                row.enabled = False
                row.operator("svg_layers.export_batch_glb", text="Select Folder First", icon='EXPORT')


class ResetAmbientOcclusionOperator(Operator):
    """Reset all Ambient Occlusion settings"""
    bl_idname = "svg_layers.reset_ambient_occlusion"
    bl_label = "Reset AO"
    bl_description = "Reset all Ambient Occlusion settings to defaults"
    bl_options = {'UNDO'}
    
    layer_name: StringProperty()
    
    def execute(self, context):
        props = context.scene.svg_layers_props
        
        for setting in props.layer_settings:
            if setting.layer_name == self.layer_name:
                setting.use_ambient_occlusion = True
                setting.ao_samples = 256
                setting.ao_distance = 0.05
                setting.update_ambient_occlusion(context)
                # Reset AO sync setting
                props = context.scene.svg_layers_props
                props.sync_ambient_occlusion = True
                break
        
        return {'FINISHED'}

class ResetPrincipledBSDFOperator(Operator):
    """Reset Principled BSDF settings"""
    bl_idname = "svg_layers.reset_principled_bsdf"
    bl_label = "Reset BSDF"
    bl_description = "Reset Metallic and Roughness to defaults"
    bl_options = {'UNDO'}
    
    layer_name: StringProperty()
    
    def execute(self, context):
        props = context.scene.svg_layers_props
        
        for setting in props.layer_settings:
            if setting.layer_name == self.layer_name:
                setting.material_metallic = 0.0
                setting.material_roughness = 0.5
                setting.update_material_properties(context)
                # Reset Principled BSDF sync setting
                props = context.scene.svg_layers_props
                props.sync_principled_bsdf = True
                break
        
        return {'FINISHED'}

class ResetUVUnwrapOperator(Operator):
    """Reset all UV Unwrap settings"""
    bl_idname = "svg_layers.reset_uv_unwrap"
    bl_label = "Reset UV"
    bl_description = "Reset all UV Unwrap and Packing settings to defaults"
    bl_options = {'UNDO'}
    
    layer_name: StringProperty()
    
    def execute(self, context):
        props = context.scene.svg_layers_props
        
        for setting in props.layer_settings:
            if setting.layer_name == self.layer_name:
                # Reset UV method
                setting.uv_method = 'MOF'
                
                # Reset MOF settings
                setting.mof_separate_hard_edges = True
                setting.mof_separate_marked_edges = True
                setting.mof_overlap_identical = False
                setting.mof_overlap_mirrored = False
                setting.mof_world_scale = True
                setting.mof_use_normals = False
                setting.mof_suppress_validation = False
                setting.mof_smooth = False
                setting.mof_keep_original = False
                setting.mof_triangulate = False
                
                # Reset Smart UV settings
                setting.uv_angle_limit = 66.0
                setting.uv_margin_method = 'ADD'
                setting.uv_rotate_method = 'AXIS_ALIGNED'
                setting.uv_island_margin = 0.005
                setting.uv_area_weight = 0.0
                setting.uv_correct_aspect = True
                setting.uv_scale_to_bounds = False
                
                # Reset Cube Projection settings
                setting.cube_size = 1.0  # Changed to 1.0 as more logical default
                setting.cube_correct_aspect = True
                setting.cube_clip_to_bounds = False
                setting.cube_scale_to_bounds = False
                
                # Reset ALL packing settings
                setting.enable_uv_packing = True
                setting.pack_shape_method = 'CONCAVE'
                setting.pack_scale = True
                setting.pack_rotate = True
#                setting.pack_rotation_method = 'ANY'
                setting.pack_margin_method = 'ADD'
                setting.pack_margin = 0.005
                setting.pack_pin_islands = False
                setting.pack_pin_method = 'LOCKED'
                setting.pack_merge_overlapping = False
                setting.pack_udim_source = 'CLOSEST_UDIM'
                
                # Reset UV sync setting
                props.sync_uv_unwrap = True
                break
        
        return {'FINISHED'}

class ResetBakingOperator(Operator):
    """Reset all Baking settings"""
    bl_idname = "svg_layers.reset_baking"
    bl_label = "Reset Baking"
    bl_description = "Reset all Baking settings to defaults"
    bl_options = {'UNDO'}
    
    layer_name: StringProperty()
    
    def execute(self, context):
        props = context.scene.svg_layers_props
        
        for setting in props.layer_settings:
            if setting.layer_name == self.layer_name:
                setting.bake_method = 'EMIT'
                setting.bake_samples = 100
                setting.bake_margin = 32
                setting.texture_resolution = 2048
                # Reset baking sync setting
                props = context.scene.svg_layers_props
                props.sync_baking = True
                break
        
        return {'FINISHED'}

class ResetPointsRedistributionOperator(Operator):
    """Reset all Points Redistribution settings"""
    bl_idname = "svg_layers.reset_points_redistribution"
    bl_label = "Reset Points"
    bl_description = "Reset all Points Redistribution settings to defaults"
    bl_options = {'UNDO'}
    
    layer_name: StringProperty()
    
    def execute(self, context):
        props = context.scene.svg_layers_props
        
        for setting in props.layer_settings:
            if setting.layer_name == self.layer_name:
                setting.enable_points_redistribution = False
                setting.point_spacing = 0.0005
                setting.straight_removal = 100
                setting.straight_edge_tolerance = 0.0001
                setting.handle_redistribution_toggle(context)
                break
        
        return {'FINISHED'}


class ResetLayerSettingsOperator(Operator):
    """Reset all settings for this layer"""
    bl_idname = "svg_layers.reset_layer_settings"
    bl_label = "Reset Layer"
    bl_description = "Reset all settings for this layer to defaults"
    bl_options = {'UNDO'}
    
    layer_name: StringProperty()
    
    def execute(self, context):
        props = context.scene.svg_layers_props
        
        for setting in props.layer_settings:
            if setting.layer_name == self.layer_name:
                # GEOMETRY SETTINGS
                setting.extrusion_depth = 0.0
                setting.bevel_depth = 0.0
                setting.use_curve_offset = False
                setting.geometry_rotation = 0.0
                setting.geometry_rotation_last = 0.0
                setting.fill_mode = 'BOTH'
                setting.auto_adjust_layers = True
                setting.layer_z_position = 0.0
                
                # CURVE RESOLUTION
                setting.resolution_u = 16
                setting.bevel_resolution = 4
                
                # POINTS REDISTRIBUTION
                setting.enable_points_redistribution = False
                setting.point_spacing = 0.0005
                setting.straight_removal = 100
                setting.straight_edge_tolerance = 0.0001
                setting.stored_curve_data = ""
                
                # LAYER OFFSET
                setting.layer_offset_percentage = 0.0
                
                # MATERIALS
                setting.material_mode = 'PRESERVED'
                
                # AMBIENT OCCLUSION
                setting.use_ambient_occlusion = True
                setting.ao_samples = 256
                setting.ao_distance = 0.05
                
                # PRINCIPLED BSDF
                setting.material_metallic = 0.0
                setting.material_roughness = 0.5
                
                # UV UNWRAP
                setting.uv_method = 'MOF'
                setting.uv_angle_limit = 66.0
                setting.uv_margin_method = 'ADD'
                setting.uv_rotate_method = 'AXIS_ALIGNED'
                setting.uv_island_margin = 0.005
                setting.uv_area_weight = 0.0
                setting.uv_correct_aspect = True
                setting.uv_scale_to_bounds = False
                
                # Reset MOF settings
                setting.mof_separate_hard_edges = True
                setting.mof_separate_marked_edges = True
                setting.mof_overlap_identical = False
                setting.mof_overlap_mirrored = False
                setting.mof_world_scale = True
                setting.mof_use_normals = False
                setting.mof_suppress_validation = False
                setting.mof_smooth = False
                setting.mof_keep_original = False
                setting.mof_triangulate = False
                
                # PACKING
                setting.enable_uv_packing = True
                setting.pack_shape_method = 'CONCAVE'
                setting.pack_scale = True
                setting.pack_rotate = True
#                setting.pack_rotation_method = 'ANY'
                setting.pack_margin_method = 'ADD'
                setting.pack_margin = 0.005
                setting.pack_pin_islands = False
                setting.pack_pin_method = 'LOCKED'
                setting.pack_merge_overlapping = False
                setting.pack_udim_source = 'CLOSEST_UDIM'
                
                # BAKING
                setting.bake_method = 'EMIT'
                setting.bake_samples = 100
                setting.bake_margin = 32
                setting.texture_resolution = 2048
                
                # Apply updates
                setting.update_shape(context)
                setting.update_curve_settings(context)
                setting.update_layer_positions(context)
                setting.update_material_properties(context)
                setting.update_ambient_occlusion(context)
                setting.handle_redistribution_toggle(context)
                # Reset ALL sync settings when resetting entire layer
                props = context.scene.svg_layers_props
                props.sync_layer_offset = False
                props.sync_bevel_depth = False
                props.sync_material_mode = True
                props.sync_ambient_occlusion = True
                props.sync_principled_bsdf = True
                props.sync_uv_unwrap = True
                props.sync_baking = True
                props.sync_geometry_offset = False
                props.sync_geometry_rotation = False
                props.sync_resolution_u = False
                props.sync_bevel_resolution = False
                props.sync_fill_mode = False
                props.sync_auto_adjust_layers = True
                break
        
        return {'FINISHED'}

class ResetGeometrySettingsOperator(Operator):
    """Reset geometry settings for this layer"""
    bl_idname = "svg_layers.reset_geometry_settings"
    bl_label = "Reset Geometry"
    bl_description = "Reset all geometry settings to defaults"
    bl_options = {'UNDO'}
    
    layer_name: StringProperty()
    
    def execute(self, context):
        props = context.scene.svg_layers_props
        
        for setting in props.layer_settings:
            if setting.layer_name == self.layer_name:
                setting.extrusion_depth = 0.0
                setting.bevel_depth = 0.0
                setting.use_curve_offset = False
                setting.geometry_rotation = 0.0
                setting.geometry_rotation_last = 0.0
                setting.fill_mode = 'BOTH'
                setting.update_shape(context)
                setting.layer_offset_percentage = 0.0
                setting.auto_adjust_layers = True
                setting.layer_z_position = 0.0
                
                # Reset geometry-related sync settings
                props = context.scene.svg_layers_props
                props.sync_layer_offset = False
                props.sync_bevel_depth = False
                props.sync_geometry_offset = False 
                props.sync_geometry_rotation = False
                props.sync_fill_mode = False 
                props.sync_auto_adjust_layers = True
                break
        
        return {'FINISHED'}

class ResetCurveSettingsOperator(Operator):
    """Reset curve resolution settings for this layer"""
    bl_idname = "svg_layers.reset_curve_settings"
    bl_label = "Reset Curve"
    bl_description = "Reset all curve resolution settings to defaults"
    bl_options = {'UNDO'}
    
    layer_name: StringProperty()
    
    def execute(self, context):
        props = context.scene.svg_layers_props
        
        for setting in props.layer_settings:
            if setting.layer_name == self.layer_name:
                # CURVE RESOLUTION
                setting.resolution_u = 16
                setting.bevel_resolution = 4
                
                # POINTS REDISTRIBUTION (child of Curve Resolution)
                setting.enable_points_redistribution = False
                setting.point_spacing = 0.0005
                setting.straight_removal = 100
                setting.straight_edge_tolerance = 0.0001
                setting.stored_curve_data = ""
                
                setting.update_curve_settings(context)
                setting.handle_redistribution_toggle(context)
                
                # Reset curve-related sync settings
                props.sync_resolution_u = False  # Add this
                props.sync_bevel_resolution = False  # Add this
                break
        
        return {'FINISHED'}


class ResetMaterialsSettingsOperator(Operator):
    """Reset materials settings for this layer"""
    bl_idname = "svg_layers.reset_materials_settings"
    bl_label = "Reset Materials"
    bl_description = "Reset all materials settings to defaults"
    bl_options = {'UNDO'}
    
    layer_name: StringProperty()
    
    def execute(self, context):
        props = context.scene.svg_layers_props
        
        for setting in props.layer_settings:
            if setting.layer_name == self.layer_name:
                # MATERIALS
                setting.material_mode = 'PRESERVED'
                
                # AMBIENT OCCLUSION
                setting.use_ambient_occlusion = True
                setting.ao_samples = 256
                setting.ao_distance = 0.05
                
                # PRINCIPLED BSDF
                setting.material_metallic = 0.0
                setting.material_roughness = 0.5
                
                # UV UNWRAP
                setting.uv_method = 'MOF'
                setting.uv_angle_limit = 66.0
                setting.uv_margin_method = 'ADD'
                setting.uv_rotate_method = 'AXIS_ALIGNED'
                setting.uv_island_margin = 0.005
                setting.uv_area_weight = 0.0
                setting.uv_correct_aspect = True
                setting.uv_scale_to_bounds = False
                
                # Reset MOF settings
                setting.mof_separate_hard_edges = True
                setting.mof_separate_marked_edges = True
                setting.mof_overlap_identical = False
                setting.mof_overlap_mirrored = False
                setting.mof_world_scale = True
                setting.mof_use_normals = False
                setting.mof_suppress_validation = False
                setting.mof_smooth = False
                setting.mof_keep_original = False
                setting.mof_triangulate = False
                
                # PACKING (child of UV Unwrap)
                setting.enable_uv_packing = True
                setting.pack_shape_method = 'CONCAVE'
                setting.pack_scale = True
                setting.pack_rotate = True
#                setting.pack_rotation_method = 'ANY'
                setting.pack_margin_method = 'ADD'
                setting.pack_margin = 0.005
                setting.pack_pin_islands = False
                setting.pack_pin_method = 'LOCKED'
                setting.pack_merge_overlapping = False
                setting.pack_udim_source = 'CLOSEST_UDIM'
                
                # BAKING
                setting.bake_method = 'EMIT'
                setting.bake_samples = 100
                setting.bake_margin = 32
                setting.texture_resolution = 2048
                
                # Update materials
                setting.update_material_properties(context)
                setting.update_ambient_occlusion(context)
                # Reset materials and all child sync settings
                props = context.scene.svg_layers_props
                props.sync_material_mode = True
                props.sync_ambient_occlusion = True
                props.sync_principled_bsdf = True
                props.sync_uv_unwrap = True
                props.sync_baking = True
                break
        
        return {'FINISHED'}

class ResetPropertyOperator(Operator):
    """Reset individual property to default"""
    bl_idname = "svg_layers.reset_property"
    bl_label = "Reset"
    bl_description = "Reset to default value"
    bl_options = {'UNDO'}
    
    layer_name: StringProperty()
    property_name: StringProperty()
    
    def execute(self, context):
        props = context.scene.svg_layers_props
        
        for setting in props.layer_settings:
            if setting.layer_name == self.layer_name:
                # Reset specific property to default
                if self.property_name == "extrusion_depth":
                    setting.extrusion_depth = 0.0
                elif self.property_name == "bevel_depth":
                    setting.bevel_depth = 0.0
                    # Reset sync setting
                    props.sync_bevel_depth = False
                elif self.property_name == "use_curve_offset":
                    setting.use_curve_offset = False
                    props.sync_geometry_offset = False
                elif self.property_name == "geometry_rotation":
                    setting.geometry_rotation = 0.0
                    setting.geometry_rotation_last = 0.0
                    props.sync_geometry_rotation = False
                elif self.property_name == "resolution_u":
                    setting.resolution_u = 16
                    props.sync_resolution_u = False
                elif self.property_name == "bevel_resolution":
                    setting.bevel_resolution = 4
                    props.sync_bevel_resolution = False
                elif self.property_name == "enable_points_redistribution":
                    setting.enable_points_redistribution = False
                    setting.handle_redistribution_toggle(context)
                elif self.property_name == "point_spacing":
                    setting.point_spacing = 0.0005
                elif self.property_name == "straight_removal":
                    setting.straight_removal = 100
                elif self.property_name == "straight_edge_tolerance":
                    setting.straight_edge_tolerance = 0.0001
                elif self.property_name == "layer_offset_percentage":
                    setting.layer_offset_percentage = 0.0
                elif self.property_name == "layer_z_position":
                    setting.layer_z_position = 0.0
                    setting.update_z_position(context)
                elif self.property_name == "fill_mode":
                    setting.fill_mode = 'BOTH'
                    setting.update_fill_mode(context)
                    props.sync_fill_mode = False
                elif self.property_name == "auto_adjust_layers":
                    setting.auto_adjust_layers = True
                    setting.update_layer_positions(context)
                    # Reset sync setting
                    props.sync_auto_adjust_layers = True
                    # Reset sync setting
                    props.sync_layer_offset = False
                elif self.property_name == "use_ambient_occlusion":
                    setting.use_ambient_occlusion = True
                    setting.update_ambient_occlusion(context)
                elif self.property_name == "ao_samples":
                    setting.ao_samples = 256
                    setting.update_ao_settings(context)
                elif self.property_name == "ao_distance":
                    setting.ao_distance = 0.05
                    setting.update_ao_settings(context)
                elif self.property_name == "material_metallic":
                    setting.material_metallic = 0.0
                    setting.update_material_properties(context)
                elif self.property_name == "material_roughness":
                    setting.material_roughness = 0.5
                    setting.update_material_properties(context)
                elif self.property_name == "show_uv_settings":
                    setting.show_uv_settings = False
                elif self.property_name == "uv_angle_limit":
                    setting.uv_angle_limit = 66.0
                elif self.property_name == "uv_island_margin":
                    setting.uv_island_margin = 0.005
                elif self.property_name == "uv_area_weight":
                    setting.uv_area_weight = 0.0
                elif self.property_name == "show_baking_settings":
                    setting.show_baking_settings = False
                elif self.property_name == "bake_samples":
                    setting.bake_samples = 100
                elif self.property_name == "bake_margin":
                    setting.bake_margin = 32
                elif self.property_name == "texture_resolution":
                    setting.texture_resolution = 2048
                elif self.property_name == "cube_size":
                    setting.cube_size = 0.711
                elif self.property_name == "cube_correct_aspect":
                    setting.cube_correct_aspect = True
                elif self.property_name == "cube_clip_to_bounds":
                    setting.cube_clip_to_bounds = False
                elif self.property_name == "cube_scale_to_bounds":
                    setting.cube_scale_to_bounds = False
                
                # Update if needed
                if self.property_name in ["extrusion_depth", "bevel_depth", "use_curve_offset", "geometry_rotation"]:
                    setting.update_shape(context)
                elif self.property_name in ["resolution_u", "bevel_resolution"]:
                    setting.update_curve_settings(context)
                elif self.property_name == "layer_offset_percentage":
                    setting.update_layer_positions(context)
                elif self.property_name == "uv_method":
                    setting.uv_method = 'MOF'
                    
                    # Reset ALL MOF settings
                    setting.mof_separate_hard_edges = True
                    setting.mof_separate_marked_edges = True
                    setting.mof_overlap_identical = False
                    setting.mof_overlap_mirrored = False
                    setting.mof_world_scale = True
                    setting.mof_use_normals = False
                    setting.mof_suppress_validation = False
                    setting.mof_smooth = False
                    setting.mof_keep_original = False
                    setting.mof_triangulate = False
                    
                    # Reset ALL Smart UV settings
                    setting.uv_angle_limit = 66.0
                    setting.uv_margin_method = 'ADD'
                    setting.uv_rotate_method = 'AXIS_ALIGNED'
                    setting.uv_island_margin = 0.005
                    setting.uv_area_weight = 0.0
                    setting.uv_correct_aspect = True
                    setting.uv_scale_to_bounds = False
                    
                    # Reset ALL Cube Projection settings
                    setting.cube_size = 1.0
                    setting.cube_correct_aspect = True
                    setting.cube_clip_to_bounds = False
                    setting.cube_scale_to_bounds = False
                elif self.property_name == "uv_margin_method":
                    setting.uv_margin_method = 'ADD'
                elif self.property_name == "uv_rotate_method":
                    setting.uv_rotate_method = 'AXIS_ALIGNED'
                elif self.property_name == "uv_correct_aspect":
                    setting.uv_correct_aspect = True
                elif self.property_name == "uv_scale_to_bounds":
                    setting.uv_scale_to_bounds = False
                elif self.property_name == "enable_uv_packing":
                    setting.enable_uv_packing = True
                elif self.property_name == "pack_shape_method":
                    setting.pack_shape_method = 'CONCAVE'
                elif self.property_name == "pack_scale":
                    setting.pack_scale = True
                elif self.property_name == "pack_rotate":
                    setting.pack_rotate = True
#                elif self.property_name == "pack_rotation_method":
#                    setting.pack_rotation_method = 'ANY'
                elif self.property_name == "pack_margin_method":
                    setting.pack_margin_method = 'ADD'
                elif self.property_name == "pack_pin_islands":
                    setting.pack_pin_islands = False
                elif self.property_name == "pack_pin_method":
                    setting.pack_pin_method = 'LOCKED'
                elif self.property_name == "pack_merge_overlapping":
                    setting.pack_merge_overlapping = False
                elif self.property_name == "pack_udim_source":
                    setting.pack_udim_source = 'CLOSEST_UDIM'
                elif self.property_name == "bake_method":
                    setting.bake_method = 'EMIT'
                
                break
        
        return {'FINISHED'}


class ResetMaterialModeOperator(Operator):
    """Reset material mode to Preserved"""
    bl_idname = "svg_layers.reset_material_mode"
    bl_label = "Reset Material Mode"
    bl_description = "Reset material mode to Preserved"
    bl_options = {'UNDO'}
    
    layer_name: StringProperty()
    
    def execute(self, context):
        props = context.scene.svg_layers_props
        
        for setting in props.layer_settings:
            if setting.layer_name == self.layer_name:
                setting.material_mode = 'PRESERVED'
                # Reset material mode sync setting
                props = context.scene.svg_layers_props
                props.sync_material_mode = True
                break
        
        return {'FINISHED'}


class ResetPackingOperator(Operator):
    """Reset all UV Packing settings"""
    bl_idname = "svg_layers.reset_packing"
    bl_label = "Reset Packing"
    bl_description = "Reset all UV Packing settings to defaults"
    bl_options = {'UNDO'}
    
    layer_name: StringProperty()
    
    def execute(self, context):
        props = context.scene.svg_layers_props
        
        for setting in props.layer_settings:
            if setting.layer_name == self.layer_name:
                setting.enable_uv_packing = True
                setting.pack_shape_method = 'CONCAVE'
                setting.pack_scale = True
                setting.pack_rotate = True
#                setting.pack_rotation_method = 'ANY'
                setting.pack_margin_method = 'ADD'
                setting.pack_margin = 0.005
                setting.pack_pin_islands = False
                setting.pack_pin_method = 'LOCKED'
                setting.pack_merge_overlapping = False
                setting.pack_udim_source = 'CLOSEST_UDIM'
                break
        
        return {'FINISHED'}


class ToggleMaterialModeSyncOperator(Operator):
    """Toggle synchronization for material mode across all layers"""
    bl_idname = "svg_layers.toggle_material_mode_sync"
    bl_label = "Toggle Material Mode Sync"
    bl_description = "Toggle synchronization of material mode across all layers"
    bl_options = {'UNDO'}
    
    layer_name: StringProperty()  # Add this
    
    def execute(self, context):
        props = context.scene.svg_layers_props
        
        # Toggle the sync state
        if not hasattr(props, 'sync_material_mode'):
            props.sync_material_mode = True
        else:
            props.sync_material_mode = not props.sync_material_mode
        
        # If turning ON sync, apply this layer's material mode to all others
        if props.sync_material_mode:
            # Find the source layer
            source_layer = None
            for layer_setting in props.layer_settings:
                if layer_setting.layer_name == self.layer_name:
                    source_layer = layer_setting
                    break
            
            # Apply to all other layers
            if source_layer:
                for other_setting in props.layer_settings:
                    if other_setting.layer_name != self.layer_name:
                        other_setting.material_mode = source_layer.material_mode
        
        # Force UI redraw
        for area in context.screen.areas:
            if area.type == 'VIEW_3D':
                area.tag_redraw()
        
        return {'FINISHED'}


class ResetAllLayersOperator(Operator):
    """Reset all settings for all layers to defaults"""
    bl_idname = "svg_layers.reset_all_layers"
    bl_label = "Reset All Layers"
    bl_description = "Reset all settings for all layers to defaults"
    bl_options = {'UNDO'}
    
    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)
    
    def execute(self, context):
        props = context.scene.svg_layers_props
        
        for setting in props.layer_settings:
            # VISIBILITY STATES
            setting.show_layer = False
            setting.show_expanded = False
            setting.show_offset_expanded = False
            setting.show_curve_expanded = False
            setting.show_materials_expanded = False
            setting.show_ao_settings = False
            setting.show_uv_settings = False
            setting.show_packing_settings = False
            setting.show_baking_settings = False
            
            # GEOMETRY SETTINGS
            setting.extrusion_depth = 0.0
            setting.bevel_depth = 0.0
            setting.use_curve_offset = False
            setting.geometry_rotation = 0.0
            setting.geometry_rotation_last = 0.0
            setting.fill_mode = 'BOTH'
            setting.auto_adjust_layers = True
            setting.layer_z_position = 0.0
            
            # CURVE RESOLUTION
            setting.resolution_u = 16
            setting.bevel_resolution = 4
            
            # POINTS REDISTRIBUTION
            setting.enable_points_redistribution = False
            setting.point_spacing = 0.0005
            setting.straight_removal = 100
            setting.straight_edge_tolerance = 0.0001
            setting.stored_curve_data = ""
            
            # LAYER OFFSET
            setting.layer_offset_percentage = 0.0
            
            # MATERIALS
            setting.material_mode = 'PRESERVED'
            
            # AMBIENT OCCLUSION
            setting.use_ambient_occlusion = True
            setting.ao_samples = 256
            setting.ao_distance = 0.05
            
            # PRINCIPLED BSDF
            setting.material_metallic = 0.0
            setting.material_roughness = 0.5
            
            # UV UNWRAP
            setting.uv_method = 'MOF'
            setting.uv_angle_limit = 66.0
            setting.uv_margin_method = 'ADD'
            setting.uv_rotate_method = 'AXIS_ALIGNED'
            setting.uv_island_margin = 0.005
            setting.uv_area_weight = 0.0
            setting.uv_correct_aspect = True
            setting.uv_scale_to_bounds = False
            
            # Reset MOF settings
            setting.mof_separate_hard_edges = True
            setting.mof_separate_marked_edges = True
            setting.mof_overlap_identical = False
            setting.mof_overlap_mirrored = False
            setting.mof_world_scale = True
            setting.mof_use_normals = False
            setting.mof_suppress_validation = False
            setting.mof_smooth = False
            setting.mof_keep_original = False
            setting.mof_triangulate = False
            
            # PACKING
            setting.enable_uv_packing = True
            setting.pack_shape_method = 'CONCAVE'
            setting.pack_scale = True
            setting.pack_rotate = True
#            setting.pack_rotation_method = 'ANY'
            setting.pack_margin_method = 'ADD'
            setting.pack_margin = 0.005
            setting.pack_pin_islands = False
            setting.pack_pin_method = 'LOCKED'
            setting.pack_merge_overlapping = False
            setting.pack_udim_source = 'CLOSEST_UDIM'
            
            # BAKING
            setting.bake_method = 'EMIT'
            setting.bake_samples = 100
            setting.bake_margin = 32
            setting.texture_resolution = 2048
            
            # Apply updates
            setting.update_shape(context)
            setting.update_curve_settings(context)
            setting.update_layer_positions(context)
            setting.update_material_properties(context)
            setting.update_ambient_occlusion(context)
            setting.handle_redistribution_toggle(context)
        
        # Also reset global sync settings
        props.sync_ambient_occlusion = True
        props.sync_principled_bsdf = True
        props.sync_uv_unwrap = True
        props.sync_baking = True
        props.sync_layer_offset = False
        props.sync_bevel_depth = False
        props.sync_geometry_offset = False 
        props.sync_geometry_rotation = False  
        props.sync_resolution_u = False  
        props.sync_bevel_resolution = False
        props.sync_fill_mode = False
        props.sync_auto_adjust_layers = True
        props.sync_z_position = False
        
        self.report({'INFO'}, f"Reset all settings for {len(props.layer_settings)} layers")
        return {'FINISHED'}


class ToggleSyncOperator(Operator):
    """Toggle synchronization for material settings"""
    bl_idname = "svg_layers.toggle_sync"
    bl_label = "Toggle Sync"
    bl_description = "Toggle synchronization across all layers"
    bl_options = {'UNDO'}
    
    sync_type: StringProperty()
    layer_name: StringProperty()  # Add this to identify which layer
    
    def execute(self, context):
        props = context.scene.svg_layers_props
        
        # Find the source layer (where sync was toggled)
        source_layer = None
        for layer_setting in props.layer_settings:
            if layer_setting.layer_name == self.layer_name:
                source_layer = layer_setting
                break
        
        if not source_layer:
            return {'CANCELLED'}
        
        if self.sync_type == 'AMBIENT_OCCLUSION':
            props.sync_ambient_occlusion = not props.sync_ambient_occlusion
            # If turning ON sync, apply this layer's values to all others
            if props.sync_ambient_occlusion:
                for other_setting in props.layer_settings:
                    if other_setting.layer_name != self.layer_name:
                        other_setting.use_ambient_occlusion = source_layer.use_ambient_occlusion
                        other_setting.ao_samples = source_layer.ao_samples
                        other_setting.ao_distance = source_layer.ao_distance
                        
        elif self.sync_type == 'PRINCIPLED_BSDF':
            props.sync_principled_bsdf = not props.sync_principled_bsdf
            # If turning ON sync, apply this layer's values to all others
            if props.sync_principled_bsdf:
                for other_setting in props.layer_settings:
                    if other_setting.layer_name != self.layer_name:
                        other_setting.material_metallic = source_layer.material_metallic
                        other_setting.material_roughness = source_layer.material_roughness
                        
        elif self.sync_type == 'UV_UNWRAP':
            props.sync_uv_unwrap = not props.sync_uv_unwrap
            # If turning ON sync, apply this layer's UV settings to all others
            if props.sync_uv_unwrap:
                for other_setting in props.layer_settings:
                    if other_setting.layer_name != self.layer_name:
                        # Copy ALL UV settings
                        # MOF settings
                        other_setting.mof_separate_hard_edges = source_layer.mof_separate_hard_edges
                        other_setting.mof_separate_marked_edges = source_layer.mof_separate_marked_edges
                        other_setting.mof_overlap_identical = source_layer.mof_overlap_identical
                        other_setting.mof_overlap_mirrored = source_layer.mof_overlap_mirrored
                        other_setting.mof_world_scale = source_layer.mof_world_scale
                        other_setting.mof_use_normals = source_layer.mof_use_normals
                        other_setting.mof_suppress_validation = source_layer.mof_suppress_validation
                        other_setting.mof_smooth = source_layer.mof_smooth
                        other_setting.mof_keep_original = source_layer.mof_keep_original
                        other_setting.mof_triangulate = source_layer.mof_triangulate
                        # Smart UV Project
                        other_setting.uv_angle_limit = source_layer.uv_angle_limit
                        other_setting.uv_margin_method = source_layer.uv_margin_method
                        other_setting.uv_rotate_method = source_layer.uv_rotate_method
                        other_setting.uv_island_margin = source_layer.uv_island_margin
                        other_setting.uv_area_weight = source_layer.uv_area_weight
                        other_setting.uv_correct_aspect = source_layer.uv_correct_aspect
                        other_setting.uv_scale_to_bounds = source_layer.uv_scale_to_bounds
                        # Cube Projection settings
                        other_setting.cube_size = source_layer.cube_size
                        other_setting.cube_correct_aspect = source_layer.cube_correct_aspect
                        other_setting.cube_clip_to_bounds = source_layer.cube_clip_to_bounds
                        other_setting.cube_scale_to_bounds = source_layer.cube_scale_to_bounds
                        # Packing settings
                        other_setting.enable_uv_packing = source_layer.enable_uv_packing
                        other_setting.pack_shape_method = source_layer.pack_shape_method
                        other_setting.pack_scale = source_layer.pack_scale
                        other_setting.pack_rotate = source_layer.pack_rotate
#                        other_setting.pack_rotation_method = source_layer.pack_rotation_method
                        other_setting.pack_margin_method = source_layer.pack_margin_method
                        other_setting.pack_margin = source_layer.pack_margin
                        other_setting.pack_pin_islands = source_layer.pack_pin_islands
                        other_setting.pack_pin_method = source_layer.pack_pin_method
                        other_setting.pack_merge_overlapping = source_layer.pack_merge_overlapping
                        other_setting.pack_udim_source = source_layer.pack_udim_source
                        
        elif self.sync_type == 'BAKING':
            props.sync_baking = not props.sync_baking
            # If turning ON sync, apply this layer's baking settings to all others
            if props.sync_baking:
                for other_setting in props.layer_settings:
                    if other_setting.layer_name != self.layer_name:
                        other_setting.bake_method = source_layer.bake_method
                        other_setting.bake_samples = source_layer.bake_samples
                        other_setting.bake_margin = source_layer.bake_margin
                        other_setting.texture_resolution = source_layer.texture_resolution
        
        elif self.sync_type == 'LAYER_OFFSET':
            props.sync_layer_offset = not props.sync_layer_offset
            # If turning ON sync, apply this layer's values to all others
            if props.sync_layer_offset:
                for other_setting in props.layer_settings:
                    if other_setting.layer_name != self.layer_name and other_setting.layer_name != "Layer 1":
                        other_setting.layer_offset_percentage = source_layer.layer_offset_percentage
                            
        elif self.sync_type == 'BEVEL_DEPTH':
            props.sync_bevel_depth = not props.sync_bevel_depth
            if props.sync_bevel_depth:
                for other_setting in props.layer_settings:
                    if other_setting.layer_name != self.layer_name:
                        other_setting.bevel_depth = source_layer.bevel_depth
        
        elif self.sync_type == 'GEOMETRY_OFFSET':
            props.sync_geometry_offset = not props.sync_geometry_offset
            if props.sync_geometry_offset:
                for other_setting in props.layer_settings:
                    if other_setting.layer_name != self.layer_name:
                        other_setting.use_curve_offset = source_layer.use_curve_offset
                        
        elif self.sync_type == 'GEOMETRY_ROTATION':
            props.sync_geometry_rotation = not props.sync_geometry_rotation
            if props.sync_geometry_rotation:
                for other_setting in props.layer_settings:
                    if other_setting.layer_name != self.layer_name:
                        other_setting.geometry_rotation = source_layer.geometry_rotation
                        
        elif self.sync_type == 'RESOLUTION_U':
            props.sync_resolution_u = not props.sync_resolution_u
            if props.sync_resolution_u:
                for other_setting in props.layer_settings:
                    if other_setting.layer_name != self.layer_name:
                        other_setting.resolution_u = source_layer.resolution_u
                        
        elif self.sync_type == 'BEVEL_RESOLUTION':
            props.sync_bevel_resolution = not props.sync_bevel_resolution
            if props.sync_bevel_resolution:
                for other_setting in props.layer_settings:
                    if other_setting.layer_name != self.layer_name:
                        other_setting.bevel_resolution = source_layer.bevel_resolution
        
        elif self.sync_type == 'FILL_MODE':
            props.sync_fill_mode = not props.sync_fill_mode
            if props.sync_fill_mode:
                for other_setting in props.layer_settings:
                    if other_setting.layer_name != self.layer_name:
                        other_setting.fill_mode = source_layer.fill_mode
        
        elif self.sync_type == 'AUTO_ADJUST_LAYERS':
            props.sync_auto_adjust_layers = not props.sync_auto_adjust_layers
            # If turning ON sync, apply this layer's value to all others
            if props.sync_auto_adjust_layers:
                for other_setting in props.layer_settings:
                    if other_setting.layer_name != self.layer_name:
                        other_setting.auto_adjust_layers = source_layer.auto_adjust_layers
        
        elif self.sync_type == 'Z_POSITION':
            props.sync_z_position = not props.sync_z_position
            if props.sync_z_position:
                for other_setting in props.layer_settings:
                    if other_setting.layer_name != self.layer_name and other_setting.layer_name != "Layer 1":
                        # Check if this other layer also needs manual positioning
                        other_layer_number = int(other_setting.layer_name.split()[-1]) if other_setting.layer_name.split()[-1].isdigit() else 0
                        if other_layer_number > 1:
                            prev_layer_name = f"Layer {other_layer_number - 1}"
                            prev_auto_adjust = True
                            for prev_setting in props.layer_settings:
                                if prev_setting.layer_name == prev_layer_name:
                                    prev_auto_adjust = prev_setting.auto_adjust_layers
                                    break
                            
                            # Only sync if this layer also uses Z Position
                            if not prev_auto_adjust:
                                other_setting.layer_z_position = source_layer.layer_z_position
        
        # Force UI redraw
        for area in context.screen.areas:
            if area.type == 'VIEW_3D':
                area.tag_redraw()
        
        return {'FINISHED'}


# Registration
classes = [
    LayerGeometrySettings,
    SVGLayersProperties,
    ApplyPointsRedistributionOperator,
    SelectTestingFolderOperator,
    ClearTestingFolderOperator,
    ImportTestingSVGsOperator,
    SelectExportFileOperator,
    ClearExportFolderOperator,
    ExportLayersGLBOperator,
    ExportBatchGLBOperator,
    RemoveImportedModelOperator,
    ResetLayerSettingsOperator,
    ResetGeometrySettingsOperator,
    ResetCurveSettingsOperator,
    ResetMaterialsSettingsOperator,
    ResetAmbientOcclusionOperator,
    ResetPrincipledBSDFOperator,
    ResetUVUnwrapOperator,
    ResetBakingOperator,
    ResetPointsRedistributionOperator,
    ResetPackingOperator,
    ResetPropertyOperator,
    ToggleSyncOperator,
    SavePresetOperator,
    DeletePresetOperator,
    ExportPresetOperator,
    ImportPresetOperator,
    ResetMaterialModeOperator,
    ToggleMaterialModeSyncOperator,
    ResetAllLayersOperator,
    VIEW3D_PT_svg_layers_main,
]


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    
    bpy.types.Scene.svg_layers_props = PointerProperty(type=SVGLayersProperties)
    bpy.app.handlers.depsgraph_update_post.append(on_depsgraph_update)

def unregister():
    # Remove the depsgraph handler
    if on_depsgraph_update in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(on_depsgraph_update)
    
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
    
    del bpy.types.Scene.svg_layers_props


if __name__ == "__main__":
    register()