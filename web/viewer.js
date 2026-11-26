import * as THREE from 'three';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';

const defaultBounds = [[-8, -2, -8], [8, 4, 8]];
const directions = { top: [0, 1, 0], side: [1, 0, 0], iso: [0.85, 0.64, 1] };

export async function readJson(url) {
  const response = await fetch(url);
  if (!response.ok) throw new Error(`HTTP ${response.status} — ${url}`);
  return response.json();
}

function matrix(rows) {
  return new THREE.Matrix4().set(...rows.flat());
}

function box(bounds) {
  return new THREE.Box3(...bounds.map(value => new THREE.Vector3(...value)));
}

async function meshBuffer(url) {
  const response = await fetch(url);
  if (!response.ok) throw new Error(`Failed to load mesh (${response.status})`);
  return response.arrayBuffer();
}

function loadOnce(requests, url, load) {
  if (!requests.has(url)) requests.set(url, load(url));
  return requests.get(url);
}

function geometry(resource, data) {
  const positionEnd = resource.vertices * 3 * 4;
  const indicesEnd = positionEnd + resource.triangles * 3 * 4;
  const uvEnd = indicesEnd + resource.triangles * 3 * 2 * 4;
  if (data.byteLength !== uvEnd + (resource.normals ? resource.vertices * 3 * 4 : 0)) {
    throw new Error('Binary buffer size does not match the manifest');
  }
  const vertices = new Float32Array(data, 0, resource.vertices * 3);
  const indices = new Uint32Array(data, positionEnd, resource.triangles * 3);
  const expanded = new Float32Array(indices.length * 3);
  for (let index = 0; index < indices.length; index++) {
    const start = indices[index] * 3;
    expanded.set(vertices.subarray(start, start + 3), index * 3);
  }
  const result = new THREE.BufferGeometry();
  result.setAttribute('position', new THREE.BufferAttribute(expanded, 3));
  result.setAttribute('uv', new THREE.BufferAttribute(new Float32Array(data, indicesEnd, resource.triangles * 6), 2));
  if (resource.normals) {
    const native = new Float32Array(data, uvEnd);
    const normals = new Float32Array(expanded.length);
    for (let i = 0; i < indices.length; i++) normals.set(native.subarray(indices[i] * 3, indices[i] * 3 + 3), i * 3);
    result.setAttribute('normal', new THREE.BufferAttribute(normals, 3));
  } else result.computeVertexNormals();
  result.boundingBox = box(resource.bounds);
  return result;
}

function cameraFromRecord(record) {
  const camera = record.type === 'ORTHO' ? new THREE.OrthographicCamera() : new THREE.PerspectiveCamera();
  const [left, right, bottom, top, near, far] = record.planes;
  if (record.type === 'ORTHO') camera.projectionMatrix.makeOrthographic(left, right, top, bottom, near, far);
  else camera.projectionMatrix.makePerspective(left, right, top, bottom, near, far);
  camera.projectionMatrixInverse.copy(camera.projectionMatrix).invert();
  camera.near = near;
  camera.far = far;
  camera.matrixAutoUpdate = false;
  camera.matrix.copy(matrix(record.matrix));
  camera.updateMatrixWorld(true);
  return camera;
}

function disposeTree(root) {
  const released = new Set();
  root.traverse(node => {
    if (node.geometry) released.add(node.geometry);
    for (const material of [node.material].flat().filter(Boolean)) {
      released.add(material);
      for (const value of Object.values(material)) if (value?.isTexture) released.add(value);
    }
  });
  for (const resource of released) resource.dispose();
}

export class Viewer {
  constructor(canvas) {
    this.renderer = new THREE.WebGLRenderer({ canvas, antialias: true, preserveDrawingBuffer: true });
    this.scene = new THREE.Scene();
    this.scene.background = new THREE.Color('#202b3b');
    this.scene.add(new THREE.HemisphereLight('#e5f0ff', '#56505a', 2.1));
    const sun = new THREE.DirectionalLight('#fff0d7', 2.2);
    sun.position.set(-7, 12, 5);
    this.scene.add(sun);
    this.neutral = new THREE.MeshStandardMaterial({ color: '#a8b5c5', roughness: 0.83, side: THREE.DoubleSide });
    this.navigationCamera = new THREE.OrthographicCamera();
    this.camera = this.navigationCamera;
    this.meshes = new Map();
    this.geometries = new Map();
    this.placements = new Map();
    this.sectionMaterials = new Map();
    this.pendingDraw = null;
  }

  async setManifest(manifest, { fit = !this.manifest } = {}) {
    if (manifest.schema !== 1 || manifest.space !== 'three-y-up-metres') {
      throw new Error('Unsupported manifest version');
    }
    const groups = new Map();
    for (const item of manifest.instances) {
      if (!item.model) continue;
      if (!groups.has(item.model)) groups.set(item.model, []);
      groups.get(item.model).push(item);
    }
    const meshRequests = new Map();
    const imageRequests = new Map();
    const imageLoader = new THREE.ImageLoader();
    const pending = [...groups.keys()].filter(key => !this.geometries.has(key));
    const resources = await Promise.all(pending.map(async key => {
      const model = manifest.models[key];
      const [data, images] = await Promise.all([
        loadOnce(meshRequests, model.url, meshBuffer),
        Promise.all((model.materials ?? []).map(({ url }) =>
          url ? loadOnce(imageRequests, url, path => imageLoader.loadAsync(path)) : null)),
      ]);
      return { key, model, data, images };
    }));
    for (const { key, model, data, images } of resources) {
      const meshGeometry = geometry(model, data);
      this.geometries.set(key, meshGeometry);
      if (images.some(Boolean)) {
        const bindings = new Map();
        const materials = model.materials.map((material, index) => {
          if (!images[index]) return this.neutral;
          const tex = new THREE.Texture(images[index]);
          tex.colorSpace = THREE.SRGBColorSpace;
          tex.wrapS = tex.wrapT = THREE.RepeatWrapping;
          tex.needsUpdate = true;
          bindings.set(material.material, index + 1);
          return new THREE.MeshStandardMaterial({
            map: tex, alphaTest: material.alpha_test ?? 0, roughness: 0.85, side: THREE.DoubleSide,
          });
        });
        const materialAt = new Uint32Array(model.triangles);
        for (const section of model.sections) {
          materialAt.fill(bindings.get(section.material) ?? 0, section.first_triangle,
                          section.first_triangle + section.triangle_count);
        }
        let first = 0;
        for (let i = 1; i <= materialAt.length; i++) {
          if (materialAt[i] === materialAt[first]) continue;
          meshGeometry.addGroup(first * 3, (i - first) * 3, materialAt[first]);
          first = i;
        }
        this.sectionMaterials.set(key, [this.neutral, ...materials]);
      }
    }
    for (const [key, group] of this.meshes) {
      if (groups.has(key)) continue;
      this.dropGroup(key, group);
    }
    this.placements.clear();
    for (const [key, items] of groups) {
      let group = this.meshes.get(key);
      if (!group || group.items.length !== items.length || items.some((item, i) => group.items[i].id !== item.id)) {
        if (group) this.dropGroup(key, group, false);
        const mesh = new THREE.InstancedMesh(this.geometries.get(key), this.sectionMaterials.get(key) ?? this.neutral,
                                             items.length);
        group = { mesh, items: [] };
        this.scene.add(mesh);
        this.meshes.set(key, group);
      }
      let moved = false;
      items.forEach((item, index) => {
        const old = group.items[index];
        if (!old || item.matrix.some((row, r) => row.some((value, c) => value !== old.matrix[r][c]))) {
          group.mesh.setMatrixAt(index, matrix(item.matrix));
          moved = true;
        }
        this.placements.set(item.id, { mesh: group.mesh, index, item });
      });
      if (moved) {
        group.mesh.instanceMatrix.needsUpdate = true;
        group.mesh.computeBoundingSphere();
        group.mesh.computeBoundingBox();
      }
      group.items = items;
    }
    await this.setEnvironment(manifest.environment);
    this.manifest = manifest;
    if (fit) this.frame();
    this.invalidate();
  }

  dropGroup(key, group, disposeGeometry = true) {
    group.mesh.removeFromParent();
    group.mesh.dispose();
    this.meshes.delete(key);
    if (!disposeGeometry) return;
    this.geometries.get(key).dispose();
    this.geometries.delete(key);
    for (const material of this.sectionMaterials.get(key) ?? []) {
      if (material !== this.neutral) {
        material.map.dispose();
        material.dispose();
      }
    }
    this.sectionMaterials.delete(key);
  }

  async setEnvironment(environment) {
    if (environment?.url === this.environmentUrl && this.environment &&
        this.environment.position.x === environment.island_position[0]) return;
    if (this.environment) {
      this.environment.removeFromParent();
      disposeTree(this.environment);
      this.environment = null;
    }
    this.environmentUrl = environment?.url;
    if (!environment) return;
    const gltf = await new GLTFLoader().loadAsync(environment.url);
    const ground = gltf.scene;
    ground.position.set(...environment.island_position);
    ground.scale.set(...environment.island_scale);
    ground.traverse(node => {
      for (const material of [node.material].flat().filter(Boolean)) material.side = THREE.DoubleSide;
    });
    const group = new THREE.Group();
    group.add(ground);
    group.position.x = environment.island_position[0];
    ground.position.x = 0;
    const [minX, minY, maxX, maxY] = environment.water_bounds_m;
    const water = new THREE.Mesh(new THREE.PlaneGeometry(maxX - minX, maxY - minY),
      new THREE.MeshStandardMaterial({ color: '#335978', roughness: 0.72, side: THREE.DoubleSide }));
    water.rotation.x = -Math.PI / 2;
    water.position.set((minX + maxX) / 2, environment.water_z_m, (minY + maxY) / 2);
    group.add(water);
    this.scene.add(group);
    this.environment = group;
  }

  resize(width, height) {
    width = Math.max(1, width);
    height = Math.max(1, height);
    this.renderer.setSize(width, height, false);
    this.viewport = [width, height];
    if (this.span) {
      const aspect = width / height;
      const camera = this.navigationCamera;
      camera.left = -this.span * Math.max(1, aspect) / 2;
      camera.right = -camera.left;
      camera.bottom = -this.span * Math.max(1, 1 / aspect) / 2;
      camera.top = -camera.bottom;
      camera.updateProjectionMatrix();
    }
    this.invalidate();
  }

  frame(preset = 'iso', { ids, island = false } = {}) {
    const bounds = new THREE.Box3();
    for (const id of ids ?? []) {
      const target = this.placements.get(id);
      if (target) bounds.union(target.mesh.geometry.boundingBox.clone().applyMatrix4(matrix(target.item.matrix)));
    }
    if (bounds.isEmpty()) bounds.copy(box(this.manifest?.bounds ?? defaultBounds));
    if (island && this.manifest?.environment) {
      const [a, b, c, d] = this.manifest.environment.island_bounds_m;
      const [bottom, top] = this.manifest.environment.island_z_range_m;
      bounds.union(box([[a, bottom, b], [c, top, d]]));
    }
    const center = bounds.getCenter(new THREE.Vector3());
    this.span = Math.max(1, 2.42 * bounds.getBoundingSphere(new THREE.Sphere()).radius);
    this.frameCenter = center;
    const camera = this.navigationCamera;
    this.camera = camera;
    camera.up.set(...(preset === 'top' ? [0, 0, -1] : [0, 1, 0]));
    camera.position.copy(center).addScaledVector(new THREE.Vector3(...directions[preset]).normalize(), this.span);
    camera.near = 0.01;
    camera.far = Math.max(3000, this.span * 4);
    camera.zoom = 1;
    camera.lookAt(center);
    this.resize(...(this.viewport ?? [1280, 720]));
  }

  useCamera(record) {
    this.camera = cameraFromRecord(record);
    this.invalidate();
  }

  invalidate() {
    if (this.pendingDraw !== null) return;
    this.pendingDraw = requestAnimationFrame(() => this.draw());
  }

  draw() {
    if (this.pendingDraw !== null) cancelAnimationFrame(this.pendingDraw);
    this.pendingDraw = null;
    this.renderer.render(this.scene, this.camera);
    const { render, memory } = this.renderer.info;
    return { draw_calls: render.calls, triangles: render.triangles, geometries: memory.geometries,
             textures: memory.textures };
  }

  clear() {
    for (const [key, group] of this.meshes) this.dropGroup(key, group);
    this.placements.clear();
    this.setEnvironment(null);
    this.manifest = null;
    this.draw();
  }
}
