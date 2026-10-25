/* Realistic look for NitoCAD's 3D viewers (Studio and the landing demo).

   An STL has no colour and no smooth shading, only triangles. The old viewers painted every part
   one flat orange and showed each triangle as its own facet, so curved parts looked faceted and
   toy-like. This gives them what a CAD viewer gives you:

     * a metal or plastic finish picked from the part's material, lit by an image-based studio
       environment, so reflections and highlights look like a real machined part
     * smooth shading across curved surfaces, with creased normals so sharp edges stay sharp
     * thin dark outlines on the real edges, the way CAD tools draw them
     * a soft contact shadow on the ground
     * filmic tone mapping so highlights roll off instead of clipping

   Everything is built from three.js and its bundled examples, no extra downloads or assets. */
import * as THREE from "three";
import { RoomEnvironment } from "three/addons/environments/RoomEnvironment.js";
import { toCreasedNormals } from "three/addons/utils/BufferGeometryUtils.js";

const SMOOTH_ANGLE = Math.PI / 6;   // surfaces meeting at less than 30 degrees shade smoothly
const EDGE_ANGLE = 30;              // degrees: sharper corners than this get an outline

/* First match wins, so "stainless steel" is not mistaken for plain steel and "ABS" is not found inside other words. */
const FINISHES = [
  [/stainless|inox|316|304/i, { color: 0xb4b9bf, metalness: 0.9, roughness: 0.3 }],
  [/titanium/i, { color: 0x9a9fa5, metalness: 0.85, roughness: 0.36 }],
  [/brass|bronze/i, { color: 0xc9a45c, metalness: 0.9, roughness: 0.3 }],
  [/copper/i, { color: 0xb87333, metalness: 0.92, roughness: 0.27 }],
  [/steel|iron/i, { color: 0x8d949c, metalness: 0.88, roughness: 0.34 }],
  [/\b(abs|pla|petg|nylon|pom|delrin|acetal|polycarbonate|resin|plastic)\b/i, { color: 0xe3e5e8, metalness: 0, roughness: 0.55 }],
];
const ALUMINUM = { color: 0xcdd1d7, metalness: 0.85, roughness: 0.36 };

export function finishFor(materialText) {
  const t = String(materialText || "");
  const hit = FINISHES.find(([re]) => re.test(t));
  return hit ? hit[1] : ALUMINUM;   // machined aluminium unless the part says otherwise
}

export function materialFor(materialText) {
  const f = finishFor(materialText);
  return new THREE.MeshStandardMaterial({
    color: f.color, metalness: f.metalness, roughness: f.roughness, envMapIntensity: 1.1,
    // push the surface back a hair so the edge outlines never z-fight with it
    polygonOffset: true, polygonOffsetFactor: 1, polygonOffsetUnits: 1,
  });
}

/* Lighting, shadow and tone mapping. Call once, right after the renderer and scene exist. */
export function applyRealisticLook(scene, renderer, keyLight) {
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.toneMappingExposure = 1.05;
  renderer.shadowMap.enabled = true;
  renderer.shadowMap.type = THREE.PCFSoftShadowMap;

  const pmrem = new THREE.PMREMGenerator(renderer);
  const room = new RoomEnvironment();
  scene.environment = pmrem.fromScene(room, 0.04).texture;
  pmrem.dispose();

  keyLight.castShadow = true;
  keyLight.shadow.mapSize.set(2048, 2048);
  const cam = keyLight.shadow.camera;
  cam.left = -80; cam.right = 80; cam.top = 80; cam.bottom = -80; cam.near = 10; cam.far = 320;
  cam.updateProjectionMatrix();
  keyLight.shadow.bias = -0.0004;
  keyLight.shadow.normalBias = 0.05;

  const ground = new THREE.Mesh(new THREE.PlaneGeometry(600, 600), new THREE.ShadowMaterial({ opacity: 0.2 }));
  ground.rotation.x = -Math.PI / 2;
  ground.position.y = -0.05;
  ground.receiveShadow = true;
  scene.add(ground);
  return ground;
}

/* Smooth the shading of an STL, keeping real corners sharp. STL stores one flat normal per
   triangle, which is why curved parts look faceted. Returns a new geometry. */
export function prepareStlGeometry(geo) {
  geo.deleteAttribute("normal");
  try {
    return toCreasedNormals(geo, SMOOTH_ANGLE);
  } catch (e) {            // a broken mesh must still show, just with plain shading
    geo.computeVertexNormals();
    return geo;
  }
}

/* The part itself: a shaded mesh plus its edge outlines. The geometry must already be centred and
   rotated, because the outlines are built from it. */
export function buildPartObject(geo, materialText) {
  const group = new THREE.Group();
  const mesh = new THREE.Mesh(geo, materialFor(materialText));
  mesh.castShadow = true;
  group.add(mesh);
  const lines = new THREE.LineSegments(
    new THREE.EdgesGeometry(geo, EDGE_ANGLE),
    new THREE.LineBasicMaterial({ color: 0x1f2630, transparent: true, opacity: 0.5 }),
  );
  lines.name = "edges";
  group.add(lines);
  return group;
}

/* Wireframe view: show the triangles and hide the outlines. */
export function setWireframe(root, on) {
  root.traverse((o) => {
    if (o.isMesh) o.material.wireframe = on;
    if (o.isLineSegments) o.visible = !on;
  });
}

export function disposeObject(root) {
  root.traverse((o) => {
    if (o.geometry) o.geometry.dispose();
    if (o.material) (Array.isArray(o.material) ? o.material : [o.material]).forEach((m) => m.dispose());
  });
}
