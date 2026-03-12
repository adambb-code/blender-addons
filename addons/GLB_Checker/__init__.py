bl_info = {
    "name": "GLB Checker",
    "author": "Daniel Marcin",
    "version": (1, 0, 0),
    "blender": (4, 5, 0),
    "location": "View3D > Sidebar > GLB Checker",
    "description": "Import and check GLB files one by one with rotation animation",
    "category": "Import-Export",
}

import bpy
import os
from bpy.props import StringProperty, PointerProperty, FloatProperty, IntProperty, BoolProperty, FloatVectorProperty
from bpy.types import Panel, Operator, PropertyGroup
from bpy.app.handlers import persistent

def update_x_rotation(self, context):
    """Update the X rotation of the current GLB object"""
    # Find the current imported object
    initial_objects = context.scene.get("glb_checker_initial_objects", [])
    for obj in bpy.data.objects:
        if obj.name not in initial_objects:
            obj.rotation_euler[0] = self.x_rotation
            break

def update_z_rotation(self, context):
    """Update the timeline position based on Z rotation slider"""
    # Only update if animation is not playing
    if not context.screen.is_animation_playing:
        # Map rotation (0 to 2π) to timeline (1 to frame_end)
        normalized_rotation = self.z_rotation / 6.28319  # 0 to 1
        
        # Calculate frame position
        frame_start = context.scene.frame_start
        frame_end = context.scene.frame_end
        frame_range = frame_end - frame_start
        
        # Set current frame
        target_frame = int(frame_start + (normalized_rotation * frame_range))
        context.scene.frame_set(target_frame)

def update_animation_speed(self, context):
    """Update the timeline length and move the last keyframe"""
    # Calculate frame count based on slider value
    # Slider goes from 0.01 to 2.0
    # 0.01 = 1200 frames (slowest), 2.0 = 30 frames (fastest)
    # Linear interpolation between these values
    frame_count = int(1200 - ((self.animation_speed - 0.01) / (2.0 - 0.01) * (1200 - 30)))
    
    # Update timeline
    context.scene.frame_end = frame_count
    
    # Find the current imported object
    initial_objects = context.scene.get("glb_checker_initial_objects", [])
    for obj in bpy.data.objects:
        if obj.name not in initial_objects:
            # Update the last keyframe position
            if obj.animation_data and obj.animation_data.action:
                for fcurve in obj.animation_data.action.fcurves:
                    if fcurve.data_path == "rotation_euler" and fcurve.array_index == 2:
                        # Remove the last keyframe
                        if len(fcurve.keyframe_points) > 1:
                            fcurve.keyframe_points.remove(fcurve.keyframe_points[-1])
                        
                        # Add new keyframe at the new last frame
                        obj.rotation_euler[2] = 6.28319  # 360 degrees
                        obj.keyframe_insert(data_path="rotation_euler", index=2, frame=frame_count)
                        
                        # Set interpolation to linear
                        for keyframe in fcurve.keyframe_points:
                            keyframe.interpolation = 'LINEAR'
            break

def update_z_rotation_from_timeline(scene):
    """Update Z rotation slider based on current frame"""
    # Get the glb_checker properties
    if hasattr(scene, 'glb_checker'):
        props = scene.glb_checker
        
        # Calculate rotation based on current frame
        frame_start = scene.frame_start
        frame_end = scene.frame_end
        frame_current = scene.frame_current
        
        if frame_end > frame_start:
            # Normalize frame position to 0-1
            normalized_position = (frame_current - frame_start) / (frame_end - frame_start)
            # Convert to rotation (0 to 2π)
            props.z_rotation = normalized_position * 6.28319

def update_world_background(self, context):
    """Re‑wire the World nodes when the Custom Color toggle or colour value changes."""
    world = context.scene.world
    if not world:
        return
    world.use_nodes = True

    nodes  = world.node_tree.nodes
    links  = world.node_tree.links

    # ────────────────────────────────────────────────
    # Helper – (re)connect two sockets safely
    # ────────────────────────────────────────────────
    def relink(from_socket, to_socket):
        while to_socket.links:                    # strip anything that is there first
            links.remove(to_socket.links[0])
        links.new(from_socket, to_socket)

    # Locate existing key nodes (create if missing when needed)
    output = next((n for n in nodes if n.type == 'OUTPUT_WORLD'), None)
    if not output:
        output = nodes.new(type='ShaderNodeOutputWorld')
        output.location = (400, 0)

    hdri_bg = None
    white_bg = None
    mix      = None
    light    = None

    for n in nodes:
        if n.type == 'BACKGROUND':
            if any(l.from_node.type == 'TEX_ENVIRONMENT' for l in n.inputs['Color'].links):
                hdri_bg = n
            else:
                white_bg = n
        elif n.type == 'MIX_SHADER':
            mix = n
        elif n.type == 'LIGHT_PATH':
            light = n

    # Make sure the mandatory HDRI background exists
    if not hdri_bg:
        hdri_bg = nodes.new(type='ShaderNodeBackground')
        hdri_bg.location = (-200, 0)
    hdri_bg.inputs['Strength'].default_value = 1.0     # keep it bright

    # === 1)  Custom colour ON  ==========================================
    if self.use_custom_color:

        # Create missing nodes --------------------------------------------------
        if not mix:
            mix = nodes.new(type='ShaderNodeMixShader')
            mix.location = (150, 0)
        if not light:
            light = nodes.new(type='ShaderNodeLightPath')
            light.location = (-600, 200)
        if not white_bg:
            white_bg = nodes.new(type='ShaderNodeBackground')
            white_bg.location = (-200, -150)

        # Update colour on every change ----------------------------------------
        white_bg.inputs['Color'].default_value = (*self.background_color, 1.0)
        white_bg.inputs['Strength'].default_value = 1.0

        # (Re)‑establish all required links ------------------------------------
        relink(light.outputs['Is Camera Ray'], mix.inputs['Fac'])
        relink(hdri_bg.outputs['Background'],  mix.inputs[1])
        relink(white_bg.outputs['Background'], mix.inputs[2])
        relink(mix.outputs['Shader'],          output.inputs['Surface'])

    # === 2)  Custom colour OFF  =========================================
    else:
        # Simply pipe the HDRI background straight to the output
        relink(hdri_bg.outputs['Background'], output.inputs['Surface'])

def get_model_validation_data(context):
    """Get validation data for the current model"""
    initial_objects = context.scene.get("glb_checker_initial_objects", [])
    
    vertex_count = 0
    material_count = 0
    max_texture_resolution = 0
    
    # Find the current imported objects
    imported_objects = []
    for obj in bpy.data.objects:
        if obj.name not in initial_objects:
            imported_objects.append(obj)
    
    # Count vertices and materials from all mesh objects
    unique_materials = set()
    for obj in imported_objects:
        if obj.type == 'MESH':
            vertex_count += len(obj.data.vertices)
            for mat in obj.data.materials:
                if mat:
                    unique_materials.add(mat.name)
    
    material_count = len(unique_materials)
    
    # Check texture resolutions
    checked_images = set()
    for mat in bpy.data.materials:
        if mat.name in unique_materials and mat.use_nodes:
            for node in mat.node_tree.nodes:
                if node.type == 'TEX_IMAGE' and node.image:
                    if node.image.name not in checked_images:
                        checked_images.add(node.image.name)
                        max_res = max(node.image.size[0], node.image.size[1])
                        max_texture_resolution = max(max_texture_resolution, max_res)
    
    return vertex_count, material_count, max_texture_resolution

def restore_initial_scene_state_shared(context, delete_state=True):
    """Restore scene to saved state with complete object recreation"""
    import json
    
    # Stop animation if playing
    if context.screen.is_animation_playing:
        bpy.ops.screen.animation_play()
    
    # Get saved complete state
    complete_state_json = context.scene.get("glb_checker_complete_state", None)
    if not complete_state_json:
        return
    
    complete_state = json.loads(complete_state_json)
    
    # First, clear everything that's NOT saved state
    for obj in list(bpy.data.objects):
        if "_SAVED_STATE" not in obj.name:
            bpy.data.objects.remove(obj, do_unlink=True)
    
    # Clean up non-saved data blocks
    for mesh in list(bpy.data.meshes):
        if "_SAVED_STATE" not in mesh.name and mesh.users == 0:
            bpy.data.meshes.remove(mesh)
    
    for mat in list(bpy.data.materials):
        if "_SAVED_STATE" not in mat.name and mat.users == 0:
            bpy.data.materials.remove(mat)
    
    for img in list(bpy.data.images):
        if "_SAVED_STATE" not in img.name and img.users == 0:
            bpy.data.images.remove(img)

    for texture in list(bpy.data.textures):
        if "_SAVED_STATE" not in texture.name and texture.users == 0:
            bpy.data.textures.remove(texture)

    for armature in list(bpy.data.armatures):
        if "_SAVED_STATE" not in armature.name and armature.users == 0:
            bpy.data.armatures.remove(armature)

    for camera in list(bpy.data.cameras):
        if "_SAVED_STATE" not in camera.name and camera.users == 0:
            bpy.data.cameras.remove(camera)

    for light in list(bpy.data.lights):
        if "_SAVED_STATE" not in light.name and light.users == 0:
            bpy.data.lights.remove(light)

    for curve in list(bpy.data.curves):
        if "_SAVED_STATE" not in curve.name and curve.users == 0:
            bpy.data.curves.remove(curve)

    for action in list(bpy.data.actions):
        if "_SAVED_STATE" not in action.name and action.users == 0:
            bpy.data.actions.remove(action)
    
    # IMPORTANT: DON'T remove worlds here! Only remove the GLB_Checker_World later
    
    # Now restore from saved collection
    saved_collection = bpy.data.collections.get("GLB_Checker_Saved_State")
    if saved_collection:
        # Create a list of objects to restore
        objects_to_restore = list(saved_collection.objects)
        
        # Dictionary to store restored objects for parent relationship restoration
        restored_objects = {}
        
        # First pass: Create all objects
        for saved_obj in objects_to_restore:
            # Get original name
            original_name = saved_obj.name.replace("_SAVED_STATE", "")
            
            # Create a duplicate
            restored_obj = saved_obj.copy()
            restored_obj.name = original_name
            
            # Duplicate and restore the object's data
            if saved_obj.data:
                original_data_name = saved_obj.data.name.replace("_SAVED_STATE", "")
                restored_data = saved_obj.data.copy()
                restored_data.name = original_data_name
                restored_obj.data = restored_data
                
                # Handle materials
                if hasattr(restored_obj.data, 'materials'):
                    for i, mat in enumerate(saved_obj.data.materials):
                        if mat and "_SAVED_STATE" in mat.name:
                            original_mat_name = mat.name.replace("_SAVED_STATE", "")
                            if original_mat_name not in bpy.data.materials:
                                restored_mat = mat.copy()
                                restored_mat.name = original_mat_name
                                restored_obj.data.materials[i] = restored_mat
                            else:
                                restored_obj.data.materials[i] = bpy.data.materials[original_mat_name]
            
            # Link to main collection
            context.collection.objects.link(restored_obj)
            
            # Store for parent relationship restoration
            restored_objects[original_name] = restored_obj
        
        # Second pass: Restore parent relationships
        for saved_obj in objects_to_restore:
            if saved_obj.parent and "_SAVED_STATE" in saved_obj.parent.name:
                original_name = saved_obj.name.replace("_SAVED_STATE", "")
                parent_original_name = saved_obj.parent.name.replace("_SAVED_STATE", "")
                if original_name in restored_objects and parent_original_name in restored_objects:
                    restored_objects[original_name].parent = restored_objects[parent_original_name]
                    restored_objects[original_name].parent_type = saved_obj.parent_type
    
    # Remove ONLY the GLB Checker world if it exists
    if "GLB_Checker_World" in bpy.data.worlds:
        bpy.data.worlds.remove(bpy.data.worlds["GLB_Checker_World"])

    # First, collect all saved worlds to avoid modifying list while iterating
    saved_worlds = []
    for world in bpy.data.worlds:
        if "_SAVED_STATE" in world.name:
            saved_worlds.append(world)

    # Restore ALL worlds from saved state
    for saved_world in saved_worlds:
        original_name = saved_world.name.replace("_SAVED_STATE", "")
        
        # Check if original world already exists
        if original_name not in bpy.data.worlds:
            # Create a copy of the saved world with the original name
            restored_world = saved_world.copy()
            restored_world.name = original_name
            
            # If the world uses nodes, make sure the node tree is properly copied
            if saved_world.use_nodes and saved_world.node_tree:
                restored_world.use_nodes = True

    # Restore the active world assignment
    if complete_state.get("active_world"):
        if complete_state["active_world"] in bpy.data.worlds:
            context.scene.world = bpy.data.worlds[complete_state["active_world"]]
    else:
        # No world was assigned before
        context.scene.world = None
    
    # Restore all other data blocks from saved state

    # First, collect all saved data blocks to avoid modifying lists while iterating
    saved_materials = [m for m in bpy.data.materials if "_SAVED_STATE" in m.name]
    saved_meshes = [m for m in bpy.data.meshes if "_SAVED_STATE" in m.name]
    saved_images = [i for i in bpy.data.images if "_SAVED_STATE" in i.name]
    saved_textures = [t for t in bpy.data.textures if "_SAVED_STATE" in t.name]
    saved_node_groups = [ng for ng in bpy.data.node_groups if "_SAVED_STATE" in ng.name]
    saved_armatures = [a for a in bpy.data.armatures if "_SAVED_STATE" in a.name]
    saved_actions = [a for a in bpy.data.actions if "_SAVED_STATE" in a.name]
    saved_cameras = [c for c in bpy.data.cameras if "_SAVED_STATE" in c.name]
    saved_lights = [l for l in bpy.data.lights if "_SAVED_STATE" in l.name]
    saved_curves = [c for c in bpy.data.curves if "_SAVED_STATE" in c.name]
    saved_speakers = [s for s in bpy.data.speakers if "_SAVED_STATE" in s.name]
    saved_lightprobes = [lp for lp in bpy.data.lightprobes if "_SAVED_STATE" in lp.name]
    saved_fonts = [f for f in bpy.data.fonts if "_SAVED_STATE" in f.name]
    saved_metaballs = [mb for mb in bpy.data.metaballs if "_SAVED_STATE" in mb.name]
    saved_lattices = [l for l in bpy.data.lattices if "_SAVED_STATE" in l.name]
    saved_grease_pencils = [gp for gp in bpy.data.grease_pencils if "_SAVED_STATE" in gp.name]

    # Restore materials
    for saved_mat in saved_materials:
        original_name = saved_mat.name.replace("_SAVED_STATE", "")
        if original_name not in bpy.data.materials:
            restored_mat = saved_mat.copy()
            restored_mat.name = original_name
            restored_mat.use_fake_user = False  # Remove fake user flag

    # Restore meshes
    for saved_mesh in saved_meshes:
        original_name = saved_mesh.name.replace("_SAVED_STATE", "")
        if original_name not in bpy.data.meshes:
            restored_mesh = saved_mesh.copy()
            restored_mesh.name = original_name
            restored_mesh.use_fake_user = False

    # Restore images
    for saved_img in saved_images:
        original_name = saved_img.name.replace("_SAVED_STATE", "")
        if original_name not in bpy.data.images:
            restored_img = saved_img.copy()
            restored_img.name = original_name
            restored_img.use_fake_user = False

    # Restore textures
    for saved_tex in saved_textures:
        original_name = saved_tex.name.replace("_SAVED_STATE", "")
        if original_name not in bpy.data.textures:
            restored_tex = saved_tex.copy()
            restored_tex.name = original_name
            restored_tex.use_fake_user = False

    # Restore node groups
    for saved_ng in saved_node_groups:
        original_name = saved_ng.name.replace("_SAVED_STATE", "")
        if original_name not in bpy.data.node_groups:
            restored_ng = saved_ng.copy()
            restored_ng.name = original_name
            restored_ng.use_fake_user = False

    # Restore armatures
    for saved_arm in saved_armatures:
        original_name = saved_arm.name.replace("_SAVED_STATE", "")
        if original_name not in bpy.data.armatures:
            restored_arm = saved_arm.copy()
            restored_arm.name = original_name
            restored_arm.use_fake_user = False

    # Restore actions
    for saved_action in saved_actions:
        original_name = saved_action.name.replace("_SAVED_STATE", "")
        if original_name not in bpy.data.actions:
            restored_action = saved_action.copy()
            restored_action.name = original_name
            restored_action.use_fake_user = False

    # Restore cameras
    for saved_cam in saved_cameras:
        original_name = saved_cam.name.replace("_SAVED_STATE", "")
        if original_name not in bpy.data.cameras:
            restored_cam = saved_cam.copy()
            restored_cam.name = original_name
            restored_cam.use_fake_user = False

    # Restore lights
    for saved_light in saved_lights:
        original_name = saved_light.name.replace("_SAVED_STATE", "")
        if original_name not in bpy.data.lights:
            restored_light = saved_light.copy()
            restored_light.name = original_name
            restored_light.use_fake_user = False

    # Restore curves
    for saved_curve in saved_curves:
        original_name = saved_curve.name.replace("_SAVED_STATE", "")
        if original_name not in bpy.data.curves:
            restored_curve = saved_curve.copy()
            restored_curve.name = original_name
            restored_curve.use_fake_user = False

    # Restore speakers
    for saved_speaker in saved_speakers:
        original_name = saved_speaker.name.replace("_SAVED_STATE", "")
        if original_name not in bpy.data.speakers:
            restored_speaker = saved_speaker.copy()
            restored_speaker.name = original_name
            restored_speaker.use_fake_user = False

    # Restore light probes
    for saved_lp in saved_lightprobes:
        original_name = saved_lp.name.replace("_SAVED_STATE", "")
        if original_name not in bpy.data.lightprobes:
            restored_lp = saved_lp.copy()
            restored_lp.name = original_name
            restored_lp.use_fake_user = False

    # Restore fonts
    for saved_font in saved_fonts:
        original_name = saved_font.name.replace("_SAVED_STATE", "")
        if original_name not in bpy.data.fonts:
            restored_font = saved_font.copy()
            restored_font.name = original_name
            restored_font.use_fake_user = False

    # Restore metaballs
    for saved_mb in saved_metaballs:
        original_name = saved_mb.name.replace("_SAVED_STATE", "")
        if original_name not in bpy.data.metaballs:
            restored_mb = saved_mb.copy()
            restored_mb.name = original_name
            restored_mb.use_fake_user = False

    # Restore lattices
    for saved_lattice in saved_lattices:
        original_name = saved_lattice.name.replace("_SAVED_STATE", "")
        if original_name not in bpy.data.lattices:
            restored_lattice = saved_lattice.copy()
            restored_lattice.name = original_name
            restored_lattice.use_fake_user = False

    # Restore grease pencils
    for saved_gp in saved_grease_pencils:
        original_name = saved_gp.name.replace("_SAVED_STATE", "")
        if original_name not in bpy.data.grease_pencils:
            restored_gp = saved_gp.copy()
            restored_gp.name = original_name
            restored_gp.use_fake_user = False

    # Handle newer Blender version data types
    if hasattr(bpy.data, 'volumes'):
        saved_volumes = [v for v in bpy.data.volumes if "_SAVED_STATE" in v.name]
        for saved_vol in saved_volumes:
            original_name = saved_vol.name.replace("_SAVED_STATE", "")
            if original_name not in bpy.data.volumes:
                restored_vol = saved_vol.copy()
                restored_vol.name = original_name
                restored_vol.use_fake_user = False

    if hasattr(bpy.data, 'hair_curves'):
        saved_hair_curves = [hc for hc in bpy.data.hair_curves if "_SAVED_STATE" in hc.name]
        for saved_hc in saved_hair_curves:
            original_name = saved_hc.name.replace("_SAVED_STATE", "")
            if original_name not in bpy.data.hair_curves:
                restored_hc = saved_hc.copy()
                restored_hc.name = original_name
                restored_hc.use_fake_user = False

    if hasattr(bpy.data, 'pointclouds'):
        saved_pointclouds = [pc for pc in bpy.data.pointclouds if "_SAVED_STATE" in pc.name]
        for saved_pc in saved_pointclouds:
            original_name = saved_pc.name.replace("_SAVED_STATE", "")
            if original_name not in bpy.data.pointclouds:
                restored_pc = saved_pc.copy()
                restored_pc.name = original_name
                restored_pc.use_fake_user = False
    
    # Restore Blender settings
    settings_json = context.scene.get("glb_checker_settings_backup", None)
    if settings_json:
        settings = json.loads(settings_json)
        
        # Restore workspace
        for workspace in bpy.data.workspaces:
            if workspace.name == settings["workspace"]:
                context.window.workspace = workspace
                break
        
        # Restore all settings
        context.scene.render.engine = settings["render_engine"]
        context.scene.eevee.taa_samples = settings["eevee_taa_samples"]
        context.scene.eevee.taa_render_samples = settings["eevee_taa_render_samples"]
        context.scene.eevee.use_taa_reprojection = settings["eevee_use_taa_reprojection"]
        context.scene.eevee.use_shadow_jitter_viewport = settings["eevee_use_shadow_jitter_viewport"]
        context.scene.eevee.use_shadows = settings["eevee_use_shadows"]
        context.scene.display_settings.display_device = settings["display_device"]
        context.scene.view_settings.view_transform = settings["view_transform"]
        context.scene.view_settings.look = settings["look"]
        context.scene.view_settings.exposure = settings["exposure"]
        context.scene.view_settings.gamma = settings["gamma"]
        context.scene.sequencer_colorspace_settings.name = settings["sequencer_colorspace"]
        context.scene.render.film_transparent = settings["film_transparent"]
        context.scene.render.fps = settings["fps"]
        context.scene.frame_start = settings["frame_start"]
        context.scene.frame_end = settings["frame_end"]
    
    # Restore viewport settings
    viewport_json = context.scene.get("glb_checker_viewport_settings", None)
    if viewport_json:
        viewport_settings = json.loads(viewport_json)
        idx = 0
        for area in context.screen.areas:
            if area.type == 'VIEW_3D':
                for space in area.spaces:
                    if space.type == 'VIEW_3D' and idx < len(viewport_settings):
                        settings = viewport_settings[idx]
                        space.overlay.show_overlays = settings["show_overlays"]
                        space.shading.type = settings["shading_type"]
                        space.region_3d.view_perspective = settings["view_perspective"]
                        idx += 1
    
    # Reset to frame 1
    context.scene.frame_set(1)
    
    # If delete_state is True, clean up
    if delete_state:
        # Remove saved collection
        if saved_collection:
            for obj in list(saved_collection.objects):
                saved_collection.objects.unlink(obj)
            bpy.data.collections.remove(saved_collection)
        
        # Remove all saved state objects and data
        for obj in list(bpy.data.objects):
            if "_SAVED_STATE" in obj.name:
                bpy.data.objects.remove(obj, do_unlink=True)
        
        for mesh in list(bpy.data.meshes):
            if "_SAVED_STATE" in mesh.name:
                bpy.data.meshes.remove(mesh)
        
        for mat in list(bpy.data.materials):
            if "_SAVED_STATE" in mat.name:
                bpy.data.materials.remove(mat)
        
        for img in list(bpy.data.images):
            if "_SAVED_STATE" in img.name:
                bpy.data.images.remove(img)

        for texture in list(bpy.data.textures):
            if "_SAVED_STATE" in texture.name:
                bpy.data.textures.remove(texture)

        for armature in list(bpy.data.armatures):
            if "_SAVED_STATE" in armature.name:
                bpy.data.armatures.remove(armature)

        for camera in list(bpy.data.cameras):
            if "_SAVED_STATE" in camera.name:
                bpy.data.cameras.remove(camera)

        for light in list(bpy.data.lights):
            if "_SAVED_STATE" in light.name:
                bpy.data.lights.remove(light)

        for curve in list(bpy.data.curves):
            if "_SAVED_STATE" in curve.name:
                bpy.data.curves.remove(curve)

        for action in list(bpy.data.actions):
            if "_SAVED_STATE" in action.name:
                bpy.data.actions.remove(action)
        
        # Remove saved state worlds
        for world in list(bpy.data.worlds):
            if "_SAVED_STATE" in world.name:
                bpy.data.worlds.remove(world)
        
        # Remove all saved state data blocks
        for data_collection in [
            bpy.data.meshes, bpy.data.materials, bpy.data.images, 
            bpy.data.textures, bpy.data.armatures, bpy.data.cameras,
            bpy.data.lights, bpy.data.curves, bpy.data.actions,
            bpy.data.node_groups, bpy.data.speakers, bpy.data.lightprobes,
            bpy.data.fonts, bpy.data.metaballs, bpy.data.lattices,
            bpy.data.grease_pencils
        ]:
            for item in list(data_collection):
                if "_SAVED_STATE" in item.name:
                    data_collection.remove(item)

        # Handle newer Blender versions data types
        if hasattr(bpy.data, 'volumes'):
            for volume in list(bpy.data.volumes):
                if "_SAVED_STATE" in volume.name:
                    bpy.data.volumes.remove(volume)

        if hasattr(bpy.data, 'hair_curves'):
            for hair_curve in list(bpy.data.hair_curves):
                if "_SAVED_STATE" in hair_curve.name:
                    bpy.data.hair_curves.remove(hair_curve)

        if hasattr(bpy.data, 'pointclouds'):
            for pointcloud in list(bpy.data.pointclouds):
                if "_SAVED_STATE" in pointcloud.name:
                    bpy.data.pointclouds.remove(pointcloud)
        
        # Clear all custom properties
        if "glb_checker_initial_objects" in context.scene:
            del context.scene["glb_checker_initial_objects"]
        if "glb_checker_files" in context.scene:
            del context.scene["glb_checker_files"]
        if "glb_checker_current_index" in context.scene:
            del context.scene["glb_checker_current_index"]
        if "glb_checker_history" in context.scene:
            del context.scene["glb_checker_history"]
        
        # Clear all properties
        if "glb_checker_complete_state" in context.scene:
            del context.scene["glb_checker_complete_state"]
        if "glb_checker_settings_backup" in context.scene:
            del context.scene["glb_checker_settings_backup"]
        if "glb_checker_viewport_settings" in context.scene:
            del context.scene["glb_checker_viewport_settings"]

class GLBCheckerProperties(PropertyGroup):
    folder_path: StringProperty(
        name="Folder Path",
        description="Choose a folder containing GLB files",
        default="",
        maxlen=1024,
        subtype='DIR_PATH'
    )
    
    x_rotation: FloatProperty(
        name="X Rotation",
        description="Rotate the current GLB on X axis",
        default=0.0,
        min=-3.14159,
        max=3.14159,
        step=10,
        subtype='ANGLE',
        update=update_x_rotation
    )
    
    z_rotation: FloatProperty(
        name="Z Rotation",
        description="Scrub through rotation animation",
        default=0.0,  # Start at 0°
        min=0.0,      # Minimum 0°
        max=6.28319,  # Maximum 2π (360°)
        step=10,
        subtype='ANGLE',
        update=update_z_rotation
    )
    
    animation_speed: FloatProperty(
        name="Animation Speed",
        description="Adjust animation speed (timeline frames)",
        default=1.0,
        min=0.01,
        max=2.0,
        step=1,
        update=update_animation_speed
    )
    
    current_view: StringProperty(
        name="Current View",
        description="Currently selected view",
        default="FRONT"
    )

    use_custom_color: BoolProperty(
        name="Background Custom Color",
        description="Use custom background color instead of HDRI for camera view",
        default=True,
        update=update_world_background
    )
    
    background_color: FloatVectorProperty(
        name="Background Color",
        description="Custom background color for camera view",
        default=(1.0, 1.0, 1.0),
        min=0.0,
        max=1.0,
        subtype='COLOR',
        update=update_world_background
    )
    
class GLB_OT_select_folder(Operator):
    """Select folder containing GLB files"""
    bl_idname = "glb_checker.select_folder"
    bl_label = "Select Folder"
    
    directory: StringProperty(
        name="Directory",
        subtype='DIR_PATH',
    )
    
    def execute(self, context):
        context.scene.glb_checker.folder_path = self.directory
        return {'FINISHED'}
    
    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

class GLB_OT_start(Operator):
    """Start checking GLB files"""
    bl_idname = "glb_checker.start"
    bl_label = "Start"
    
    def execute(self, context):
        props = context.scene.glb_checker
        
        if not props.folder_path:
            self.report({'ERROR'}, "Please select a folder first")
            return {'CANCELLED'}
        
        # First reset the addon to initial state (if saved state exists)
        if "glb_checker_complete_state" in context.scene:
            self.restore_initial_scene_state(context, delete_state=False)

        # Save complete current state
        self.save_complete_blender_state(context)

        # Store initial scene state (for compatibility)
        context.scene["glb_checker_initial_objects"] = [obj.name for obj in bpy.data.objects]

        # Now clear the ENTIRE scene before importing GLB (except saved state items)
        # Remove all objects except those with _SAVED_STATE suffix
        for obj in list(bpy.data.objects):
            if "_SAVED_STATE" not in obj.name:
                bpy.data.objects.remove(obj, do_unlink=True)

        # Remove all data blocks except those with _SAVED_STATE suffix
        for mesh in list(bpy.data.meshes):
            if "_SAVED_STATE" not in mesh.name and mesh.users == 0:
                bpy.data.meshes.remove(mesh)

        for mat in list(bpy.data.materials):
            if "_SAVED_STATE" not in mat.name and mat.users == 0:
                bpy.data.materials.remove(mat)

        for img in list(bpy.data.images):
            if "_SAVED_STATE" not in img.name and img.users == 0:
                bpy.data.images.remove(img)

        # Clean up
        bpy.ops.outliner.orphans_purge(do_recursive=True)
        
        # Store initial Blender settings before changing them
        settings_backup = {
            "workspace": context.window.workspace.name,
            "render_engine": context.scene.render.engine,
            "eevee_taa_samples": context.scene.eevee.taa_samples,
            "eevee_taa_render_samples": context.scene.eevee.taa_render_samples,
            "eevee_use_taa_reprojection": context.scene.eevee.use_taa_reprojection,
            "eevee_use_shadow_jitter_viewport": context.scene.eevee.use_shadow_jitter_viewport,
            "eevee_use_shadows": context.scene.eevee.use_shadows,
            "display_device": context.scene.display_settings.display_device,
            "view_transform": context.scene.view_settings.view_transform,
            "look": context.scene.view_settings.look,
            "exposure": context.scene.view_settings.exposure,
            "gamma": context.scene.view_settings.gamma,
            "sequencer_colorspace": context.scene.sequencer_colorspace_settings.name,
            "film_transparent": context.scene.render.film_transparent,
            "fps": context.scene.render.fps,
            "frame_start": context.scene.frame_start,
            "frame_end": context.scene.frame_end,
        }
        
        # Store viewport settings for each 3D view
        viewport_settings = []
        for area in context.screen.areas:
            if area.type == 'VIEW_3D':
                for space in area.spaces:
                    if space.type == 'VIEW_3D':
                        viewport_settings.append({
                            "show_overlays": space.overlay.show_overlays,
                            "shading_type": space.shading.type,
                            "view_perspective": space.region_3d.view_perspective
                        })
        
        import json
        context.scene["glb_checker_settings_backup"] = json.dumps(settings_backup)
        context.scene["glb_checker_viewport_settings"] = json.dumps(viewport_settings)
        
        # Switch to Layout workspace
        for workspace in bpy.data.workspaces:
            if workspace.name == "Layout":
                context.window.workspace = workspace
                break
        
        # Set render engine to EEVEE
        context.scene.render.engine = 'BLENDER_EEVEE_NEXT'
        
        # Set viewport and render samples to 64
        context.scene.eevee.taa_samples = 64
        context.scene.eevee.taa_render_samples = 64
        
        # Turn off Temporal Reprojection
        context.scene.eevee.use_taa_reprojection = False
        
        # Turn on Jittered Shadows
        context.scene.eevee.use_shadow_jitter_viewport = True
        
        # Turn off shadows
        context.scene.eevee.use_shadows = False
        
        # Set color management settings
        context.scene.display_settings.display_device = 'sRGB'
        context.scene.view_settings.view_transform = 'Standard'
        context.scene.view_settings.look = 'None'
        context.scene.view_settings.exposure = 0.0
        context.scene.view_settings.gamma = 1.2
        context.scene.sequencer_colorspace_settings.name = 'sRGB'
        
        # Turn off transparent film
        context.scene.render.film_transparent = False
        
        # Set to front view and disable overlays, switch to rendered view
        for area in context.screen.areas:
            if area.type == 'VIEW_3D':
                for space in area.spaces:
                    if space.type == 'VIEW_3D':
                        space.overlay.show_overlays = False
                        space.shading.type = 'RENDERED'
                        # Set front view
                        override = {'area': area, 'region': area.regions[-1]}
                        with context.temp_override(**override):
                            bpy.ops.view3d.view_axis(type='FRONT')
                        # Turn off orthographic
                        space.region_3d.view_perspective = 'PERSP'
        
        # Set HDRI
        self.setup_hdri(context)
        
        # Ensure the world background is properly set up based on custom color setting
        update_world_background(props, context)
        
        # Set frame rate to 60
        context.scene.render.fps = 60
        
        # Calculate initial frame count based on default animation speed
        default_speed = 1.0
        frame_count = int(1200 - ((default_speed - 0.01) / (2.0 - 0.01) * (1200 - 30)))
        
        # Set timeline
        context.scene.frame_start = 1
        context.scene.frame_end = frame_count
        
        # Create X folder if it doesn't exist
        x_folder = os.path.join(props.folder_path, "X")
        if not os.path.exists(x_folder):
            os.makedirs(x_folder)
        
        # Get list of GLB files
        glb_files = [f for f in os.listdir(props.folder_path) 
                     if f.endswith('.glb') and os.path.isfile(os.path.join(props.folder_path, f))]
        
        # Natural sort - handles numbers properly (1, 2, 10 instead of 1, 10, 2)
        import re
        def natural_sort_key(s):
            return [int(text) if text.isdigit() else text.lower() 
                    for text in re.split('([0-9]+)', s)]
        glb_files.sort(key=natural_sort_key)
        
        if not glb_files:
            self.report({'ERROR'}, "No GLB files found in the selected folder")
            return {'CANCELLED'}
        
        # Store GLB files list
        context.scene["glb_checker_files"] = glb_files
        context.scene["glb_checker_current_index"] = 0
        
        # Initialize history list as JSON string
        import json
        context.scene["glb_checker_history"] = json.dumps([])
        
        # Import first GLB
        self.import_and_setup_glb(context, glb_files[0])
        
        return {'FINISHED'}
    
    def save_complete_blender_state(self, context):
        """Save complete Blender state including all object properties and relationships"""
        import json
        
        state = {
            # Save all objects with complete properties
            "objects": {},
            # Save object relationships
            "object_parents": {},
            # Save collection assignments
            "collection_objects": {},
            # Save selection states
            "selected_objects": [obj.name for obj in context.selected_objects],
            "active_object": context.view_layer.objects.active.name if context.view_layer.objects.active else None,
            # Save all data blocks
            "meshes": [mesh.name for mesh in bpy.data.meshes],
            "materials": [mat.name for mat in bpy.data.materials],
            "images": [img.name for img in bpy.data.images],
            "textures": [tex.name for tex in bpy.data.textures],
            "node_groups": [ng.name for ng in bpy.data.node_groups],
            "armatures": [arm.name for arm in bpy.data.armatures],
            "actions": [act.name for act in bpy.data.actions],
            "cameras": [cam.name for cam in bpy.data.cameras],
            "lights": [light.name for light in bpy.data.lights],
            "curves": [curve.name for curve in bpy.data.curves],
            "speakers": [speaker.name for speaker in bpy.data.speakers],
            "lightprobes": [lp.name for lp in bpy.data.lightprobes],
            # Save ALL worlds, not just the active one
            "worlds": [world.name for world in bpy.data.worlds],
            "active_world": context.scene.world.name if context.scene.world else None,
        }
        
        # Save detailed object properties
        for obj in bpy.data.objects:
            obj_data = {
                "location": obj.location.copy()[:],
                "rotation_euler": obj.rotation_euler.copy()[:],
                "rotation_mode": obj.rotation_mode,
                "scale": obj.scale.copy()[:],
                "type": obj.type,
                "hide_viewport": obj.hide_viewport,
                "hide_render": obj.hide_render,
                "hide_select": obj.hide_select,
                "show_name": obj.show_name,
                "show_axis": obj.show_axis,
                "show_wire": obj.show_wire,
                "display_type": obj.display_type,
                "data": obj.data.name if obj.data else None,
                "data_type": type(obj.data).__name__ if obj.data else None,
            }
            
            # Save material slots
            obj_data["material_slots"] = []
            for slot in obj.material_slots:
                obj_data["material_slots"].append({
                    "material": slot.material.name if slot.material else None,
                    "link": slot.link,  # 'DATA' or 'OBJECT'
                    "name": slot.name
                })
            
            # Save modifiers
            obj_data["modifiers"] = []
            for mod in obj.modifiers:
                mod_data = {
                    "name": mod.name,
                    "type": mod.type,
                    "show_viewport": mod.show_viewport,
                    "show_render": mod.show_render,
                }
                obj_data["modifiers"].append(mod_data)
            
            # Save constraints
            obj_data["constraints"] = []
            for con in obj.constraints:
                con_data = {
                    "name": con.name,
                    "type": con.type,
                    "enabled": con.enabled,
                    "show_expanded": con.show_expanded,
                }
                obj_data["constraints"].append(con_data)
            
            state["objects"][obj.name] = obj_data
            
            # Save parent relationship
            if obj.parent:
                state["object_parents"][obj.name] = {
                    "parent": obj.parent.name,
                    "parent_type": obj.parent_type,
                    "parent_bone": obj.parent_bone,
                }
        
        # Save collection structure
        def save_collection_hierarchy(collection, path=""):
            coll_path = f"{path}/{collection.name}" if path else collection.name
            state["collection_objects"][coll_path] = [obj.name for obj in collection.objects]
            
            for child in collection.children:
                save_collection_hierarchy(child, coll_path)
        
        # Start from master collection
        save_collection_hierarchy(context.scene.collection)
        
        # Create a hidden collection to store saved state objects
        saved_collection = bpy.data.collections.new("GLB_Checker_Saved_State")
        context.scene.collection.children.link(saved_collection)

        # Exclude from view layer
        layer_collection = context.view_layer.layer_collection.children[saved_collection.name]
        layer_collection.exclude = True

        # Duplicate all objects to the saved collection with renamed data blocks
        for obj_name in state["objects"]:
            if obj_name in bpy.data.objects:
                original_obj = bpy.data.objects[obj_name]
                
                # Duplicate object
                new_obj = original_obj.copy()
                new_obj.name = obj_name + "_SAVED_STATE"
                
                # Duplicate object data (mesh, curve, etc.) if it exists
                if original_obj.data:
                    new_data = original_obj.data.copy()
                    new_data.name = original_obj.data.name + "_SAVED_STATE"
                    new_obj.data = new_data
                    
                    # Duplicate materials
                    if hasattr(new_obj.data, 'materials'):
                        for i, mat in enumerate(new_obj.data.materials):
                            if mat:
                                # Check if we already duplicated this material
                                saved_mat_name = mat.name + "_SAVED_STATE"
                                if saved_mat_name in bpy.data.materials:
                                    new_obj.data.materials[i] = bpy.data.materials[saved_mat_name]
                                else:
                                    new_mat = mat.copy()
                                    new_mat.name = saved_mat_name
                                    new_obj.data.materials[i] = new_mat
                
                # Link to saved collection
                saved_collection.objects.link(new_obj)

        # Update state to reference the saved collection
        state["saved_collection"] = saved_collection.name
        
        # Duplicate ALL worlds to preserve them
        for world in bpy.data.worlds:
            if "_SAVED_STATE" not in world.name:
                saved_world = world.copy()
                saved_world.name = world.name + "_SAVED_STATE"
                # IMPORTANT: Give it a fake user so it doesn't get purged
                saved_world.use_fake_user = True
                
        # Duplicate only UNLINKED data blocks (those not attached to any object)
        # This prevents duplication of data that's already saved with objects

        # Duplicate unlinked materials
        for material in list(bpy.data.materials):
            if "_SAVED_STATE" not in material.name and material.users == 0:
                saved_mat = material.copy()
                saved_mat.name = material.name + "_SAVED_STATE"
                saved_mat.use_fake_user = True

        # Duplicate unlinked meshes
        for mesh in list(bpy.data.meshes):
            if "_SAVED_STATE" not in mesh.name and mesh.users == 0:
                saved_mesh = mesh.copy()
                saved_mesh.name = mesh.name + "_SAVED_STATE"
                saved_mesh.use_fake_user = True

        # Duplicate unlinked images
        for image in list(bpy.data.images):
            if "_SAVED_STATE" not in image.name and image.users == 0:
                saved_img = image.copy()
                saved_img.name = image.name + "_SAVED_STATE"
                saved_img.use_fake_user = True

        # Duplicate unlinked textures
        for texture in list(bpy.data.textures):
            if "_SAVED_STATE" not in texture.name and texture.users == 0:
                saved_tex = texture.copy()
                saved_tex.name = texture.name + "_SAVED_STATE"
                saved_tex.use_fake_user = True

        # Duplicate unlinked node groups
        for node_group in list(bpy.data.node_groups):
            if "_SAVED_STATE" not in node_group.name and node_group.users == 0:
                saved_ng = node_group.copy()
                saved_ng.name = node_group.name + "_SAVED_STATE"
                saved_ng.use_fake_user = True

        # Duplicate unlinked armatures
        for armature in list(bpy.data.armatures):
            if "_SAVED_STATE" not in armature.name and armature.users == 0:
                saved_arm = armature.copy()
                saved_arm.name = armature.name + "_SAVED_STATE"
                saved_arm.use_fake_user = True

        # Duplicate unlinked actions
        for action in list(bpy.data.actions):
            if "_SAVED_STATE" not in action.name and action.users == 0:
                saved_action = action.copy()
                saved_action.name = action.name + "_SAVED_STATE"
                saved_action.use_fake_user = True

        # Duplicate unlinked cameras
        for camera in list(bpy.data.cameras):
            if "_SAVED_STATE" not in camera.name and camera.users == 0:
                saved_cam = camera.copy()
                saved_cam.name = camera.name + "_SAVED_STATE"
                saved_cam.use_fake_user = True

        # Duplicate unlinked lights
        for light in list(bpy.data.lights):
            if "_SAVED_STATE" not in light.name and light.users == 0:
                saved_light = light.copy()
                saved_light.name = light.name + "_SAVED_STATE"
                saved_light.use_fake_user = True

        # Duplicate unlinked curves
        for curve in list(bpy.data.curves):
            if "_SAVED_STATE" not in curve.name and curve.users == 0:
                saved_curve = curve.copy()
                saved_curve.name = curve.name + "_SAVED_STATE"
                saved_curve.use_fake_user = True

        # Duplicate unlinked speakers
        for speaker in list(bpy.data.speakers):
            if "_SAVED_STATE" not in speaker.name and speaker.users == 0:
                saved_speaker = speaker.copy()
                saved_speaker.name = speaker.name + "_SAVED_STATE"
                saved_speaker.use_fake_user = True

        # Duplicate unlinked light probes
        for lightprobe in list(bpy.data.lightprobes):
            if "_SAVED_STATE" not in lightprobe.name and lightprobe.users == 0:
                saved_lp = lightprobe.copy()
                saved_lp.name = lightprobe.name + "_SAVED_STATE"
                saved_lp.use_fake_user = True

        # Duplicate unlinked fonts
        for font in list(bpy.data.fonts):
            if "_SAVED_STATE" not in font.name and font.users == 0:
                saved_font = font.copy()
                saved_font.name = font.name + "_SAVED_STATE"
                saved_font.use_fake_user = True

        # Duplicate unlinked metaballs
        for metaball in list(bpy.data.metaballs):
            if "_SAVED_STATE" not in metaball.name and metaball.users == 0:
                saved_mb = metaball.copy()
                saved_mb.name = metaball.name + "_SAVED_STATE"
                saved_mb.use_fake_user = True

        # Duplicate unlinked lattices
        for lattice in list(bpy.data.lattices):
            if "_SAVED_STATE" not in lattice.name and lattice.users == 0:
                saved_lattice = lattice.copy()
                saved_lattice.name = lattice.name + "_SAVED_STATE"
                saved_lattice.use_fake_user = True

        # Duplicate unlinked grease pencils
        for grease_pencil in list(bpy.data.grease_pencils):
            if "_SAVED_STATE" not in grease_pencil.name and grease_pencil.users == 0:
                saved_gp = grease_pencil.copy()
                saved_gp.name = grease_pencil.name + "_SAVED_STATE"
                saved_gp.use_fake_user = True

        # Duplicate unlinked paint curves
        for paint_curve in list(bpy.data.paint_curves):
            if "_SAVED_STATE" not in paint_curve.name and paint_curve.users == 0:
                saved_pc = paint_curve.copy()
                saved_pc.name = paint_curve.name + "_SAVED_STATE"
                saved_pc.use_fake_user = True

        # Duplicate unlinked particles
        for particle in list(bpy.data.particles):
            if "_SAVED_STATE" not in particle.name and particle.users == 0:
                saved_part = particle.copy()
                saved_part.name = particle.name + "_SAVED_STATE"
                saved_part.use_fake_user = True

        # Duplicate unlinked movie clips
        for movie_clip in list(bpy.data.movieclips):
            if "_SAVED_STATE" not in movie_clip.name and movie_clip.users == 0:
                saved_mc = movie_clip.copy()
                saved_mc.name = movie_clip.name + "_SAVED_STATE"
                saved_mc.use_fake_user = True

        # Duplicate unlinked masks
        for mask in list(bpy.data.masks):
            if "_SAVED_STATE" not in mask.name and mask.users == 0:
                saved_mask = mask.copy()
                saved_mask.name = mask.name + "_SAVED_STATE"
                saved_mask.use_fake_user = True

        # Duplicate unlinked sounds
        for sound in list(bpy.data.sounds):
            if "_SAVED_STATE" not in sound.name and sound.users == 0:
                saved_sound = sound.copy()
                saved_sound.name = sound.name + "_SAVED_STATE"
                saved_sound.use_fake_user = True

        # Duplicate unlinked brushes
        for brush in list(bpy.data.brushes):
            if "_SAVED_STATE" not in brush.name and brush.users == 0:
                saved_brush = brush.copy()
                saved_brush.name = brush.name + "_SAVED_STATE"
                saved_brush.use_fake_user = True

        # Duplicate unlinked palettes
        for palette in list(bpy.data.palettes):
            if "_SAVED_STATE" not in palette.name and palette.users == 0:
                saved_palette = palette.copy()
                saved_palette.name = palette.name + "_SAVED_STATE"
                saved_palette.use_fake_user = True

        # Duplicate unlinked cache files
        for cache_file in list(bpy.data.cache_files):
            if "_SAVED_STATE" not in cache_file.name and cache_file.users == 0:
                saved_cf = cache_file.copy()
                saved_cf.name = cache_file.name + "_SAVED_STATE"
                saved_cf.use_fake_user = True

        # Handle newer Blender version data types (with version checks)
        if hasattr(bpy.data, 'volumes'):  # Volumes (Blender 2.91+)
            for volume in list(bpy.data.volumes):
                if "_SAVED_STATE" not in volume.name and volume.users == 0:
                    saved_vol = volume.copy()
                    saved_vol.name = volume.name + "_SAVED_STATE"
                    saved_vol.use_fake_user = True

        if hasattr(bpy.data, 'hair_curves'):  # Hair curves (Blender 3.5+)
            for hair_curve in list(bpy.data.hair_curves):
                if "_SAVED_STATE" not in hair_curve.name and hair_curve.users == 0:
                    saved_hc = hair_curve.copy()
                    saved_hc.name = hair_curve.name + "_SAVED_STATE"
                    saved_hc.use_fake_user = True

        if hasattr(bpy.data, 'pointclouds'):  # Point clouds (Blender 3.0+)
            for pointcloud in list(bpy.data.pointclouds):
                if "_SAVED_STATE" not in pointcloud.name and pointcloud.users == 0:
                    saved_pc = pointcloud.copy()
                    saved_pc.name = pointcloud.name + "_SAVED_STATE"
                    saved_pc.use_fake_user = True

        # Save as JSON
        context.scene["glb_checker_complete_state"] = json.dumps(state)

    
    def restore_initial_scene_state(self, context, delete_state=True):
        restore_initial_scene_state_shared(context, delete_state)
    
    def setup_hdri(self, context):
        # Create a NEW world for the addon (don't modify the existing one)
        world = bpy.data.worlds.new("GLB_Checker_World")
        context.scene.world = world
        
        world.use_nodes = True
        nodes = world.node_tree.nodes
        links = world.node_tree.links
        
        # Clear existing nodes
        nodes.clear()
        
        # Get properties
        props = context.scene.glb_checker
        
        if props.use_custom_color:
            # Complex setup with mix shader
            # Add Light Path node
            light_path = nodes.new(type='ShaderNodeLightPath')
            light_path.location = (-600, 200)
            
            # Add Environment Texture node
            env_texture = nodes.new(type='ShaderNodeTexEnvironment')
            env_texture.location = (-400, 0)
            
            # Add first Background node (for HDRI)
            background_hdri = nodes.new(type='ShaderNodeBackground')
            background_hdri.inputs['Strength'].default_value = 1.0
            background_hdri.location = (-200, 0)
            
            # Add second Background node (custom color background)
            background_white = nodes.new(type='ShaderNodeBackground')
            background_white.inputs['Color'].default_value = (*props.background_color, 1.0)
            background_white.inputs['Strength'].default_value = 1.0
            background_white.location = (-200, -150)
            
            # Add Mix Shader node
            mix_shader = nodes.new(type='ShaderNodeMixShader')
            mix_shader.location = (100, 0)
            
            # Add Output node
            output = nodes.new(type='ShaderNodeOutputWorld')
            output.location = (300, 0)
            
            # Try to load HDRI from addon folder
            addon_dir = os.path.dirname(os.path.abspath(__file__))
            hdri_folder = os.path.join(addon_dir, "hdri")
            
            # Look for any HDR/EXR file in the hdri folder
            if os.path.exists(hdri_folder):
                for file in os.listdir(hdri_folder):
                    if file.lower().endswith(('.hdr', '.exr', '.hdri')):
                        hdri_path = os.path.join(hdri_folder, file)
                        try:
                            hdri_image = bpy.data.images.load(hdri_path)
                            env_texture.image = hdri_image
                            break
                        except:
                            pass
            
            # Link nodes for complex setup
            links.new(light_path.outputs['Is Camera Ray'], mix_shader.inputs['Fac'])
            links.new(env_texture.outputs['Color'], background_hdri.inputs['Color'])
            links.new(background_hdri.outputs['Background'], mix_shader.inputs[1])
            links.new(background_white.outputs['Background'], mix_shader.inputs[2])
            links.new(mix_shader.outputs['Shader'], output.inputs['Surface'])
        else:
            # Simple setup - just HDRI
            # Add Environment Texture node
            env_texture = nodes.new(type='ShaderNodeTexEnvironment')
            env_texture.location = (-300, 0)
            
            # Add Background node
            background = nodes.new(type='ShaderNodeBackground')
            background.inputs['Strength'].default_value = 1.0
            background.location = (0, 0)
            
            # Add Output node
            output = nodes.new(type='ShaderNodeOutputWorld')
            output.location = (200, 0)
            
            # Try to load HDRI from addon folder
            addon_dir = os.path.dirname(os.path.abspath(__file__))
            hdri_folder = os.path.join(addon_dir, "hdri")
            
            # Look for any HDR/EXR file in the hdri folder
            if os.path.exists(hdri_folder):
                for file in os.listdir(hdri_folder):
                    if file.lower().endswith(('.hdr', '.exr', '.hdri')):
                        hdri_path = os.path.join(hdri_folder, file)
                        try:
                            hdri_image = bpy.data.images.load(hdri_path)
                            env_texture.image = hdri_image
                            break
                        except:
                            pass
            
            # Link nodes for simple setup
            links.new(env_texture.outputs['Color'], background.inputs['Color'])
            links.new(background.outputs['Background'], output.inputs['Surface'])
    
    def import_and_setup_glb(self, context, filename):
        props = context.scene.glb_checker
        
        # Reset sliders to default values
        props.x_rotation = 0.0
        props.z_rotation = 0.0  # Start at 0°
        props.animation_speed = 1.0
        
        filepath = os.path.join(props.folder_path, filename)
        
        # Import GLB
        bpy.ops.import_scene.gltf(filepath=filepath)
        
        # Get the imported object
        obj = context.active_object
        if not obj:
            initial_objects = context.scene.get("glb_checker_initial_objects", [])
            for o in bpy.data.objects:
                if o.name not in initial_objects:
                    obj = o
                    break
        
        if obj:
            # Make sure it's selected and active
            bpy.ops.object.select_all(action='DESELECT')
            obj.select_set(True)
            context.view_layer.objects.active = obj
            
            # Set rotation mode to XYZ Euler
            obj.rotation_mode = 'XYZ'
            
            # Set keyframe at frame 1
            context.scene.frame_set(1)
            obj.rotation_euler[2] = 0  # Z rotation = 0
            obj.keyframe_insert(data_path="rotation_euler", index=2)
            
            # Get current frame count from animation speed
            frame_count = int(1200 - ((props.animation_speed - 0.01) / (2.0 - 0.01) * (1200 - 30)))
            
            # Set keyframe at last frame
            context.scene.frame_set(frame_count)
            obj.rotation_euler[2] = 6.28319  # Z rotation = 360 degrees (in radians)
            obj.keyframe_insert(data_path="rotation_euler", index=2)
            
            # Set interpolation to linear
            if obj.animation_data and obj.animation_data.action:
                for fcurve in obj.animation_data.action.fcurves:
                    if fcurve.data_path == "rotation_euler" and fcurve.array_index == 2:
                        for keyframe in fcurve.keyframe_points:
                            keyframe.interpolation = 'LINEAR'
            
            # Reset to frame 1
            context.scene.frame_set(1)
            
            # Zoom to object - frame selected
            for area in context.screen.areas:
                if area.type == 'VIEW_3D':
                    override = {'area': area, 'region': area.regions[-1]}
                    with context.temp_override(**override):
                        bpy.ops.view3d.view_selected()
            
            # Play animation
            bpy.ops.screen.animation_play()

class GLB_OT_thick(Operator):
    """Mark as good and load next GLB"""
    bl_idname = "glb_checker.thick"
    bl_label = ""
    
    def execute(self, context):
        import json
        
        # Check if we've already finished processing all files
        glb_files = context.scene.get("glb_checker_files", [])
        current_index = context.scene.get("glb_checker_current_index", 0)
        
        if not glb_files or current_index >= len(glb_files):
            self.report({'INFO'}, "All GLB files have already been checked!")
            return {'FINISHED'}
        
        # Stop animation
        if context.screen.is_animation_playing:
            bpy.ops.screen.animation_play()
        
        # Save current file to history before moving on
        history_json = context.scene.get("glb_checker_history", "[]")
        history = json.loads(history_json)
        
        if current_index < len(glb_files):
            # Add to history: (filename, action, original_index)
            history.append({
                "filename": glb_files[current_index],
                "action": "thick",
                "index": current_index
            })
            context.scene["glb_checker_history"] = json.dumps(history)
        
        # Clear scene to initial state
        self.clear_to_initial_state(context)
        
        # Load next GLB
        self.load_next_glb(context)
        
        return {'FINISHED'}
    
    def clear_to_initial_state(self, context):
        initial_objects = context.scene.get("glb_checker_initial_objects", [])
        
        # Delete objects that weren't in the initial scene
        objects_to_delete = []
        for obj in bpy.data.objects:
            if obj.name not in initial_objects:
                objects_to_delete.append(obj)
        
        # Delete the objects
        for obj in objects_to_delete:
            bpy.data.objects.remove(obj, do_unlink=True)
        
        # Clean up orphaned data
        bpy.ops.outliner.orphans_purge(do_recursive=True)
    
    def load_next_glb(self, context):
        glb_files = context.scene.get("glb_checker_files", [])
        current_index = context.scene.get("glb_checker_current_index", 0)
        
        # Move to next file
        current_index += 1
        
        if current_index < len(glb_files):
            context.scene["glb_checker_current_index"] = current_index
            
            # Import and setup next GLB
            props = context.scene.glb_checker
            
            # Reset sliders to default values
            props.x_rotation = 0.0
            props.z_rotation = 0.0  # Start at 0°
            props.animation_speed = 1.0
            
            filepath = os.path.join(props.folder_path, glb_files[current_index])
            
            # Import GLB
            bpy.ops.import_scene.gltf(filepath=filepath)
            
            # Get the imported object
            obj = context.active_object
            if not obj:
                initial_objects = context.scene.get("glb_checker_initial_objects", [])
                for o in bpy.data.objects:
                    if o.name not in initial_objects:
                        obj = o
                        break
            
            if obj:
                # Make sure it's selected and active
                bpy.ops.object.select_all(action='DESELECT')
                obj.select_set(True)
                context.view_layer.objects.active = obj
                
                # Set rotation mode to XYZ Euler
                obj.rotation_mode = 'XYZ'
                
                # Set keyframe at frame 1
                context.scene.frame_set(1)
                obj.rotation_euler[2] = 0  # Z rotation = 0
                obj.keyframe_insert(data_path="rotation_euler", index=2)
                
                # Get current frame count from animation speed
                props = context.scene.glb_checker
                frame_count = int(1200 - ((props.animation_speed - 0.01) / (2.0 - 0.01) * (1200 - 30)))
                
                # Set keyframe at last frame
                context.scene.frame_set(frame_count)
                obj.rotation_euler[2] = 6.28319  # Z rotation = 360 degrees (in radians)
                obj.keyframe_insert(data_path="rotation_euler", index=2)
                
                # Set interpolation to linear
                if obj.animation_data and obj.animation_data.action:
                    for fcurve in obj.animation_data.action.fcurves:
                        if fcurve.data_path == "rotation_euler" and fcurve.array_index == 2:
                            for keyframe in fcurve.keyframe_points:
                                keyframe.interpolation = 'LINEAR'
                
                # Reset to frame 1
                context.scene.frame_set(1)
                
                # Play animation
                bpy.ops.screen.animation_play()
        else:
            self.report({'INFO'}, "All GLB files have been checked!")
            
            # Restore scene to initial state
            self.restore_initial_scene_state(context, delete_state=True)
    
    def restore_initial_scene_state(self, context, delete_state=True):
        restore_initial_scene_state_shared(context, delete_state)

class GLB_OT_x(Operator):
    """Mark as problematic, move to X folder, and load next GLB"""
    bl_idname = "glb_checker.x"
    bl_label = ""
    
    def execute(self, context):
        import shutil
        import json
        
        # Check if we've already finished processing all files
        glb_files = context.scene.get("glb_checker_files", [])
        current_index = context.scene.get("glb_checker_current_index", 0)
        
        if not glb_files or current_index >= len(glb_files):
            self.report({'INFO'}, "All GLB files have already been checked!")
            return {'FINISHED'}
        
        # Stop animation
        if context.screen.is_animation_playing:
            bpy.ops.screen.animation_play()
        
        # Get current GLB file
        glb_files = context.scene.get("glb_checker_files", [])
        current_index = context.scene.get("glb_checker_current_index", 0)
        
        if current_index < len(glb_files):
            props = context.scene.glb_checker
            current_file = glb_files[current_index]
            
            # Save to history before moving
            history_json = context.scene.get("glb_checker_history", "[]")
            history = json.loads(history_json)
            history.append({
                "filename": current_file,
                "action": "x",
                "index": current_index
            })
            context.scene["glb_checker_history"] = json.dumps(history)
            
            # Move file to X folder
            src = os.path.join(props.folder_path, current_file)
            dst = os.path.join(props.folder_path, "X", current_file)
            
            try:
                shutil.move(src, dst)
            except:
                pass
        
        # Clear scene to initial state
        self.clear_to_initial_state(context)
        
        # Load next GLB
        glb_files = context.scene.get("glb_checker_files", [])
        current_index = context.scene.get("glb_checker_current_index", 0)
        
        # Move to next file
        current_index += 1
        
        if current_index < len(glb_files):
            context.scene["glb_checker_current_index"] = current_index
            
            # Import and setup next GLB
            props = context.scene.glb_checker
            
            # Reset sliders to default values
            props.x_rotation = 0.0
            props.z_rotation = 0.0  # Start at 0°
            props.animation_speed = 1.0
            
            filepath = os.path.join(props.folder_path, glb_files[current_index])
                        
            # Import GLB
            bpy.ops.import_scene.gltf(filepath=filepath)
            
            # Get the imported object
            obj = context.active_object
            if not obj:
                initial_objects = context.scene.get("glb_checker_initial_objects", [])
                for o in bpy.data.objects:
                    if o.name not in initial_objects:
                        obj = o
                        break
            
            if obj:
                # Make sure it's selected and active
                bpy.ops.object.select_all(action='DESELECT')
                obj.select_set(True)
                context.view_layer.objects.active = obj
                
                # Set rotation mode to XYZ Euler
                obj.rotation_mode = 'XYZ'
                
                # Set keyframe at frame 1
                context.scene.frame_set(1)
                obj.rotation_euler[2] = 0  # Z rotation = 0
                obj.keyframe_insert(data_path="rotation_euler", index=2)
                
                # Get current frame count from animation speed
                props = context.scene.glb_checker
                frame_count = int(1200 - ((props.animation_speed - 0.01) / (2.0 - 0.01) * (1200 - 30)))
                
                # Set keyframe at last frame
                context.scene.frame_set(frame_count)
                obj.rotation_euler[2] = 6.28319  # Z rotation = 360 degrees (in radians)
                obj.keyframe_insert(data_path="rotation_euler", index=2)
                
                # Set interpolation to linear
                if obj.animation_data and obj.animation_data.action:
                    for fcurve in obj.animation_data.action.fcurves:
                        if fcurve.data_path == "rotation_euler" and fcurve.array_index == 2:
                            for keyframe in fcurve.keyframe_points:
                                keyframe.interpolation = 'LINEAR'
                
                # Reset to frame 1
                context.scene.frame_set(1)
                
                # Play animation
                bpy.ops.screen.animation_play()
        else:
            self.report({'INFO'}, "All GLB files have been checked!")
            
            # Restore scene to initial state
            self.restore_initial_scene_state(context, delete_state=True)
        
        return {'FINISHED'}
    
    def clear_to_initial_state(self, context):
        initial_objects = context.scene.get("glb_checker_initial_objects", [])
        
        # Delete objects that weren't in the initial scene
        objects_to_delete = []
        for obj in bpy.data.objects:
            if obj.name not in initial_objects:
                objects_to_delete.append(obj)
        
        # Delete the objects
        for obj in objects_to_delete:
            bpy.data.objects.remove(obj, do_unlink=True)
        
        # Clean up orphaned data
        bpy.ops.outliner.orphans_purge(do_recursive=True)
    
    def restore_initial_scene_state(self, context, delete_state=True):
        restore_initial_scene_state_shared(context, delete_state)

class GLB_OT_front_view(Operator):
    """Set front view (non-orthographic)"""
    bl_idname = "glb_checker.front_view"
    bl_label = "Front"
    
    def execute(self, context):
        # Set front view
        bpy.ops.view3d.view_axis(type='FRONT')
        # Turn off orthographic
        for area in context.screen.areas:
            if area.type == 'VIEW_3D':
                for space in area.spaces:
                    if space.type == 'VIEW_3D':
                        space.region_3d.view_perspective = 'PERSP'
        # Update current view
        context.scene.glb_checker.current_view = "FRONT"
        return {'FINISHED'}

class GLB_OT_top_view(Operator):
    """Set top view (non-orthographic)"""
    bl_idname = "glb_checker.top_view"
    bl_label = "Top"
    
    def execute(self, context):
        # Set top view
        bpy.ops.view3d.view_axis(type='TOP')
        # Turn off orthographic
        for area in context.screen.areas:
            if area.type == 'VIEW_3D':
                for space in area.spaces:
                    if space.type == 'VIEW_3D':
                        space.region_3d.view_perspective = 'PERSP'
        # Update current view
        context.scene.glb_checker.current_view = "TOP"
        return {'FINISHED'}

class GLB_OT_bottom_view(Operator):
    """Set bottom view (non-orthographic)"""
    bl_idname = "glb_checker.bottom_view"
    bl_label = "Bottom"
    
    def execute(self, context):
        # Set bottom view
        bpy.ops.view3d.view_axis(type='BOTTOM')
        # Turn off orthographic
        for area in context.screen.areas:
            if area.type == 'VIEW_3D':
                for space in area.spaces:
                    if space.type == 'VIEW_3D':
                        space.region_3d.view_perspective = 'PERSP'
        # Update current view
        context.scene.glb_checker.current_view = "BOTTOM"
        return {'FINISHED'}

class GLB_OT_undo(Operator):
    """Undo last action and go back to previous GLB"""
    bl_idname = "glb_checker.undo"
    bl_label = "Undo"
    
    def execute(self, context):
        import shutil
        import json
        
        # Get history
        history_json = context.scene.get("glb_checker_history", "[]")
        history = json.loads(history_json)
        
        if not history:
            return {'FINISHED'}  # Nothing to undo
        
        # Stop animation if playing
        if context.screen.is_animation_playing:
            bpy.ops.screen.animation_play()
        
        # Get the last action
        last_action = history[-1]
        filename = last_action["filename"]
        action = last_action["action"]
        original_index = last_action["index"]
        
        # Remove last action from history
        history.pop()
        context.scene["glb_checker_history"] = json.dumps(history)
        
        # If the file was moved to X folder, move it back
        if action == "x":
            props = context.scene.glb_checker
            src = os.path.join(props.folder_path, "X", filename)
            dst = os.path.join(props.folder_path, filename)
            
            try:
                if os.path.exists(src):
                    shutil.move(src, dst)
            except:
                pass
        
        # Update current index to the previous file
        context.scene["glb_checker_current_index"] = original_index
        
        # Clear current scene
        initial_objects = context.scene.get("glb_checker_initial_objects", [])
        objects_to_delete = []
        for obj in bpy.data.objects:
            if obj.name not in initial_objects:
                objects_to_delete.append(obj)
        
        for obj in objects_to_delete:
            bpy.data.objects.remove(obj, do_unlink=True)
        
        # Clean up orphaned data
        bpy.ops.outliner.orphans_purge(do_recursive=True)
        
        # Reload the previous GLB
        props = context.scene.glb_checker
        
        # Reset sliders to default values
        props.x_rotation = 0.0
        props.z_rotation = 0.0  # Start at 0°
        props.animation_speed = 1.0
        
        filepath = os.path.join(props.folder_path, filename)
        
        # Import GLB
        bpy.ops.import_scene.gltf(filepath=filepath)
        
        # Get the imported object
        obj = context.active_object
        if not obj:
            initial_objects = context.scene.get("glb_checker_initial_objects", [])
            for o in bpy.data.objects:
                if o.name not in initial_objects:
                    obj = o
                    break
        
        if obj:
            # Make sure it's selected and active
            bpy.ops.object.select_all(action='DESELECT')
            obj.select_set(True)
            context.view_layer.objects.active = obj
            
            # Set rotation mode to XYZ Euler
            obj.rotation_mode = 'XYZ'
            
            # Set keyframe at frame 1
            context.scene.frame_set(1)
            obj.rotation_euler[2] = 0  # Z rotation = 0
            obj.keyframe_insert(data_path="rotation_euler", index=2)
            
            # Get current frame count from animation speed
            frame_count = int(1200 - ((props.animation_speed - 0.01) / (2.0 - 0.01) * (1200 - 30)))
            
            # Set keyframe at last frame
            context.scene.frame_set(frame_count)
            obj.rotation_euler[2] = 6.28319  # Z rotation = 360 degrees (in radians)
            obj.keyframe_insert(data_path="rotation_euler", index=2)
            
            # Set interpolation to linear
            if obj.animation_data and obj.animation_data.action:
                for fcurve in obj.animation_data.action.fcurves:
                    if fcurve.data_path == "rotation_euler" and fcurve.array_index == 2:
                        for keyframe in fcurve.keyframe_points:
                            keyframe.interpolation = 'LINEAR'
            
            # Reset to frame 1
            context.scene.frame_set(1)
            
            # Play animation
            bpy.ops.screen.animation_play()
        
        return {'FINISHED'}

class GLB_OT_play_forward(Operator):
    """Play animation forward"""
    bl_idname = "glb_checker.play_forward"
    bl_label = "Play Forward"
    
    def execute(self, context):
        # If playing, stop first
        if context.screen.is_animation_playing:
            bpy.ops.screen.animation_play()
        
        # Now play forward
        bpy.ops.screen.animation_play()
        return {'FINISHED'}

class GLB_OT_play_reverse(Operator):
    """Play animation in reverse"""
    bl_idname = "glb_checker.play_reverse"
    bl_label = "Play Reverse"
    
    def execute(self, context):
        # If playing, stop first
        if context.screen.is_animation_playing:
            bpy.ops.screen.animation_play()
        
        # Now play reverse
        bpy.ops.screen.animation_play(reverse=True)
        return {'FINISHED'}

class GLB_OT_pause(Operator):
    """Pause animation"""
    bl_idname = "glb_checker.pause"
    bl_label = "Pause"
    
    def execute(self, context):
        # Only pause if currently playing
        if context.screen.is_animation_playing:
            bpy.ops.screen.animation_play()
        return {'FINISHED'}

class GLB_OT_reset_x_rotation(Operator):
    """Reset X rotation to default value"""
    bl_idname = "glb_checker.reset_x_rotation"
    bl_label = "Reset X Rotation"
    
    def execute(self, context):
        context.scene.glb_checker.x_rotation = 0.0
        return {'FINISHED'}

class GLB_OT_reset_z_rotation(Operator):
    """Reset rotation to start (0°)"""
    bl_idname = "glb_checker.reset_z_rotation"
    bl_label = "Reset Z Rotation"
    
    def execute(self, context):
        context.scene.glb_checker.z_rotation = 0.0  # 0°
        return {'FINISHED'}

class GLB_OT_reset_animation_speed(Operator):
    """Reset animation speed to default value"""
    bl_idname = "glb_checker.reset_animation_speed"
    bl_label = "Reset Animation Speed"
    
    def execute(self, context):
        context.scene.glb_checker.animation_speed = 1.0
        return {'FINISHED'}

class GLB_OT_reset_background_color(Operator):
    """Reset background color to default white"""
    bl_idname = "glb_checker.reset_background_color"
    bl_label = "Reset Background Color"
    
    def execute(self, context):
        context.scene.glb_checker.background_color = (1.0, 1.0, 1.0)
        return {'FINISHED'}

class GLB_OT_reset_addon(Operator):
    """Reset addon to initial state before Start was pressed"""
    bl_idname = "glb_checker.reset_addon"
    bl_label = "Reset Addon"
    
    def execute(self, context):
        # Use the enhanced restore method with delete_state=True
        self.restore_initial_scene_state(context, delete_state=True)
        self.report({'INFO'}, "Addon reset to initial state")
        return {'FINISHED'}
    
    def restore_initial_scene_state(self, context, delete_state=True):
        restore_initial_scene_state_shared(context, delete_state)

class GLB_OT_refresh_folder(Operator):
    """Refresh folder to check for new GLB files"""
    bl_idname = "glb_checker.refresh_folder"
    bl_label = "Refresh"
    
    def execute(self, context):
        props = context.scene.glb_checker
        
        if not props.folder_path:
            self.report({'ERROR'}, "No folder selected")
            return {'CANCELLED'}
        
        # Get current state
        current_files = context.scene.get("glb_checker_files", [])
        current_index = context.scene.get("glb_checker_current_index", 0)
        
        if not current_files:
            self.report({'INFO'}, "Please use Start button first")
            return {'CANCELLED'}
        
        # Scan folder for GLB files
        glb_files = [f for f in os.listdir(props.folder_path) 
                     if f.endswith('.glb') and os.path.isfile(os.path.join(props.folder_path, f))]
        
        # Natural sort
        import re
        def natural_sort_key(s):
            return [int(text) if text.isdigit() else text.lower() 
                    for text in re.split('([0-9]+)', s)]
        glb_files.sort(key=natural_sort_key)
        
        # Find new files that weren't in the original list
        new_files = [f for f in glb_files if f not in current_files]
        
        if new_files:
            # Add new files to the end of the current list
            updated_files = current_files + new_files
            context.scene["glb_checker_files"] = updated_files
            
            self.report({'INFO'}, f"Found {len(new_files)} new files. Total: {len(updated_files)}")
        else:
            self.report({'INFO'}, "No new files found")
        
        return {'FINISHED'}

class GLB_OT_not_started(Operator):
    """Addon not started"""
    bl_idname = "glb_checker.not_started"
    bl_label = "Not Started"
    
    def execute(self, context):
        self.report({'INFO'}, "Please press Start to begin processing GLB files")
        return {'FINISHED'}

class VIEW3D_PT_glb_checker(Panel):
    """Creates a Panel in the 3D viewport sidebar"""
    bl_label = "GLB Checker"
    bl_idname = "VIEW3D_PT_glb_checker"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "GLB Checker"
    
    def draw(self, context):
        layout = self.layout
        props = context.scene.glb_checker
        
        # Check if addon is active (Start has been pressed)
        is_active = "glb_checker_files" in context.scene
        
        layout.label(text="Select folder containing GLB files:")
        
        # Folder selection - always enabled
        row = layout.row(align=True)
        row.prop(props, "folder_path", text="")
        row.operator("glb_checker.refresh_folder", icon='FILE_REFRESH', text="")
        
        # Start button - make it bigger
        row = layout.row()
        row.scale_y = 1.5  # Make it taller
        row.operator("glb_checker.start", icon='PLAY')
        
        # All controls below should be disabled if not active
        
        # Thick and X buttons - make them much bigger
        row = layout.row(align=True)
        row.scale_y = 3.2  # good height
        row.enabled = is_active
        if is_active:
            row.operator("glb_checker.thick", text="✔", icon='NONE')
            row.operator("glb_checker.x", text="❌", icon='NONE')
        else:
            row.operator("glb_checker.not_started", text="✔", icon='NONE')
            row.operator("glb_checker.not_started", text="❌", icon='NONE')
        
        # Undo button
        col = layout.column()
        col.enabled = is_active
        if is_active:
            col.operator("glb_checker.undo", icon='LOOP_BACK')
        else:
            col.operator("glb_checker.not_started", text="Undo", icon='LOOP_BACK')
        
        # View buttons
        layout.separator()
        layout.label(text="Views:")
        row = layout.row(align=True)
        row.enabled = is_active
        if is_active:
            row.operator("glb_checker.front_view", depress=(props.current_view == "FRONT"))
            row.operator("glb_checker.top_view", depress=(props.current_view == "TOP"))
            row.operator("glb_checker.bottom_view", depress=(props.current_view == "BOTTOM"))
        else:
            row.operator("glb_checker.not_started", text="Front")
            row.operator("glb_checker.not_started", text="Top")
            row.operator("glb_checker.not_started", text="Bottom")
        
        # Animation controls
        layout.separator()
        layout.label(text="Animation:")
        row = layout.row(align=True)
        row.scale_y = 1.5  # Make buttons taller
        row.scale_x = 3.0  # Make buttons wider
        row.enabled = is_active
        
        if is_active:
            row.operator("glb_checker.play_reverse", text="", icon='PLAY_REVERSE')
            row.operator("glb_checker.pause", text="", icon='PAUSE')
            row.operator("glb_checker.play_forward", text="", icon='PLAY')
        else:
            row.operator("glb_checker.not_started", text="", icon='PLAY_REVERSE')
            row.operator("glb_checker.not_started", text="", icon='PAUSE')
            row.operator("glb_checker.not_started", text="", icon='PLAY')
        
        # Rotation and speed controls
        layout.separator()
        layout.label(text="Controls:")
        
        # X Rotation with reset button
        row = layout.row(align=True)
        row.enabled = is_active
        row.prop(props, "x_rotation", slider=True)
        if is_active:
            row.operator("glb_checker.reset_x_rotation", text="", icon='LOOP_BACK')
        else:
            row.operator("glb_checker.not_started", text="", icon='LOOP_BACK')
        
        # Z rotation - only enabled when animation is paused AND addon is active
        row = layout.row(align=True)
        row.enabled = is_active and not context.screen.is_animation_playing
        row.prop(props, "z_rotation", slider=True)
        if is_active:
            row.operator("glb_checker.reset_z_rotation", text="", icon='LOOP_BACK')
        else:
            row.operator("glb_checker.not_started", text="", icon='LOOP_BACK')
        
        # Animation speed with reset button
        row = layout.row(align=True)
        row.enabled = is_active
        row.prop(props, "animation_speed", slider=True)
        if is_active:
            row.operator("glb_checker.reset_animation_speed", text="", icon='LOOP_BACK')
        else:
            row.operator("glb_checker.not_started", text="", icon='LOOP_BACK')
        
        # Background settings
        layout.separator()
        col = layout.column()
        col.enabled = is_active
        col.prop(props, "use_custom_color")
        if props.use_custom_color:
            row = col.row(align=True)
            row.prop(props, "background_color", text="")
            if is_active:
                row.operator("glb_checker.reset_background_color", text="", icon='LOOP_BACK')
            else:
                row.operator("glb_checker.not_started", text="", icon='LOOP_BACK')
        
        # Reset Addon button
        col = layout.column()
        col.enabled = is_active
        if is_active:
            col.operator("glb_checker.reset_addon", icon='FILE_REFRESH')
        else:
            col.operator("glb_checker.not_started", text="Reset Addon", icon='FILE_REFRESH')
        
        # Current file info - always visible
        if context.scene.get("glb_checker_files"):
            layout.separator()
            files = context.scene.get("glb_checker_files", [])
            current = context.scene.get("glb_checker_current_index", 0)
            if current < len(files):
                # Make text bigger
                col = layout.column()
                col.scale_y = 1.0
                col.label(text=f"File Name: {files[current]}")
                col.label(text=f"Progress: {current + 1}/{len(files)}")
        
        # Model Validation Checklist - only show when active
        if is_active and context.scene.get("glb_checker_files"):
            box = layout.box()
            box.label(text="Model Validation Checklist:")
            
            # Get validation data
            vertex_count, material_count, max_texture_res = get_model_validation_data(context)
            
            # Make validation items bigger
            col = box.column(align=True)
            col.scale_y = 1.2
            
            # Vertex Count Check
            row = col.row(align=True)
            row.alignment = 'LEFT'
            
            vertex_pass = vertex_count <= 100000
            vertex_text = f"Vertex Count: {vertex_count:,}"
            
            if vertex_pass:
                row.label(text=vertex_text)
                row.label(text="", icon='CHECKMARK')  # Green checkmark icon
            else:
                row.label(text=vertex_text)
                row.alert = True
                row.label(text="", icon='CANCEL')  # Red X icon
            
            # Texture Resolution Check
            row = col.row(align=True)
            row.alignment = 'LEFT'
            
            texture_pass = max_texture_res <= 8192
            texture_text = f"Max Texture Resolution: {max_texture_res}"
            
            if texture_pass:
                row.label(text=texture_text)
                row.label(text="", icon='CHECKMARK')  # Green checkmark icon
            else:
                row.label(text=texture_text)
                row.alert = True
                row.label(text="", icon='CANCEL')  # Red X icon
            
            # Material Count Check
            row = col.row(align=True)
            row.alignment = 'LEFT'
            
            material_pass = material_count <= 3
            material_text = f"Material Count: {material_count}"
            
            if material_pass:
                row.label(text=material_text)
                row.label(text="", icon='CHECKMARK')  # Green checkmark icon
            else:
                row.label(text=material_text)
                row.alert = True
                row.label(text="", icon='CANCEL')  # Red X icon

# Registration
classes = [
    GLBCheckerProperties,
    GLB_OT_select_folder,
    GLB_OT_start,
    GLB_OT_thick,
    GLB_OT_x,
    GLB_OT_undo,
    GLB_OT_refresh_folder,
    GLB_OT_reset_addon,
    GLB_OT_reset_x_rotation,
    GLB_OT_reset_z_rotation,
    GLB_OT_reset_animation_speed,
    GLB_OT_reset_background_color,
    GLB_OT_front_view,
    GLB_OT_top_view,
    GLB_OT_bottom_view,
    GLB_OT_play_forward,
    GLB_OT_play_reverse,
    GLB_OT_pause,
    GLB_OT_not_started,
    VIEW3D_PT_glb_checker,
]

def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    
    bpy.types.Scene.glb_checker = PointerProperty(type=GLBCheckerProperties)
    
    # Add frame change handler to force UI redraw
    if glb_checker_frame_change not in bpy.app.handlers.frame_change_post:
        bpy.app.handlers.frame_change_post.append(glb_checker_frame_change)

def unregister():
    for cls in classes:
        bpy.utils.unregister_class(cls)
    
    del bpy.types.Scene.glb_checker
    
    # Remove frame change handler
    if glb_checker_frame_change in bpy.app.handlers.frame_change_post:
        bpy.app.handlers.frame_change_post.remove(glb_checker_frame_change)

@persistent
def glb_checker_frame_change(scene):
    # Update Z rotation slider
    update_z_rotation_from_timeline(scene)
    
    # Force UI redraw on frame change
    for area in bpy.context.screen.areas:
        if area.type == 'VIEW_3D':
            for region in area.regions:
                if region.type == 'UI':
                    region.tag_redraw()

if __name__ == "__main__":
    register()