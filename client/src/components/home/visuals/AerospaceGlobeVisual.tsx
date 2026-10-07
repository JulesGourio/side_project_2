import { useEffect, useRef, useState } from "react";
import * as THREE from "three";

interface AerospaceGlobeVisualProps {
  isVisible: boolean;
}

// Latécoère global facility locations
const locationNodes = [
  // Europe — HQ & major plants
  { lat: 43.6047, lon: 1.4442, size: 1.0 }, // Toulouse (HQ)
  { lat: 50.0755, lon: 14.4378, size: 0.7 }, // Prague
  { lat: 42.6977, lon: 23.3219, size: 0.6 }, // Sofia
  { lat: 48.8566, lon: 2.3522, size: 0.6 }, // Paris
  { lat: 51.5074, lon: -0.1278, size: 0.5 }, // London
  { lat: 50.1109, lon: 8.6821, size: 0.5 }, // Frankfurt
  { lat: 41.9028, lon: 12.4964, size: 0.4 }, // Rome
  { lat: 40.4168, lon: -3.7038, size: 0.4 }, // Madrid

  // Americas
  { lat: 29.0729, lon: -110.9559, size: 0.7 }, // Hermosillo
  { lat: 45.5017, lon: -73.5673, size: 0.6 }, // Montreal
  { lat: 33.749, lon: -84.388, size: 0.5 }, // Atlanta
  { lat: -23.5505, lon: -46.6333, size: 0.5 }, // São Paulo

  // Middle East
  { lat: 25.2048, lon: 55.2708, size: 0.6 }, // Dubai
  { lat: 25.2854, lon: 51.531, size: 0.5 }, // Doha

  // Asia-Pacific
  { lat: 1.3521, lon: 103.8198, size: 0.6 }, // Singapore
  { lat: 35.6762, lon: 139.6503, size: 0.6 }, // Tokyo
  { lat: 22.3193, lon: 114.1694, size: 0.5 }, // Hong Kong
  { lat: 28.6139, lon: 77.209, size: 0.5 }, // Delhi

  // Africa & Oceania
  { lat: 33.5731, lon: -7.5898, size: 0.6 }, // Casablanca
  { lat: 36.7528, lon: 3.042, size: 0.5 }, // Algiers
  { lat: -33.8688, lon: 151.2093, size: 0.5 }, // Sydney
];

// Flight arc connections between facilities
const flightConnections = [
  // Toulouse hub connections
  { from: 0, to: 1 }, // Toulouse - Prague
  { from: 0, to: 2 }, // Toulouse - Sofia
  { from: 0, to: 3 }, // Toulouse - Paris
  { from: 0, to: 8 }, // Toulouse - Hermosillo
  { from: 0, to: 9 }, // Toulouse - Montreal
  { from: 0, to: 12 }, // Toulouse - Dubai
  { from: 0, to: 18 }, // Toulouse - Casablanca
  { from: 0, to: 14 }, // Toulouse - Singapore

  // European network
  { from: 3, to: 4 }, // Paris - London
  { from: 3, to: 5 }, // Paris - Frankfurt
  { from: 1, to: 5 }, // Prague - Frankfurt
  { from: 1, to: 2 }, // Prague - Sofia

  // Trans-Atlantic
  { from: 4, to: 10 }, // London - Atlanta
  { from: 3, to: 9 }, // Paris - Montreal
  { from: 7, to: 11 }, // Madrid - São Paulo
  { from: 0, to: 10 }, // Toulouse - Atlanta

  // Middle East - Asia
  { from: 12, to: 14 }, // Dubai - Singapore
  { from: 12, to: 15 }, // Dubai - Tokyo
  { from: 13, to: 17 }, // Doha - Delhi
  { from: 12, to: 17 }, // Dubai - Delhi

  // Asia-Pacific
  { from: 14, to: 16 }, // Singapore - Hong Kong
  { from: 15, to: 16 }, // Tokyo - Hong Kong
  { from: 14, to: 20 }, // Singapore - Sydney
  { from: 15, to: 20 }, // Tokyo - Sydney

  // Africa routes
  { from: 18, to: 19 }, // Casablanca - Algiers
  { from: 3, to: 18 }, // Paris - Casablanca
  { from: 3, to: 19 }, // Paris - Algiers

  // Long-haul
  { from: 9, to: 15 }, // Montreal - Tokyo
  { from: 10, to: 12 }, // Atlanta - Dubai
  { from: 14, to: 17 }, // Singapore - Delhi
  { from: 5, to: 15 }, // Frankfurt - Tokyo
];

function latLonToVector3(
  lat: number,
  lon: number,
  radius: number,
): THREE.Vector3 {
  const phi = (90 - lat) * (Math.PI / 180);
  const theta = (lon + 180) * (Math.PI / 180);
  const x = -radius * Math.sin(phi) * Math.cos(theta);
  const y = radius * Math.cos(phi);
  const z = radius * Math.sin(phi) * Math.sin(theta);
  return new THREE.Vector3(x, y, z);
}

function createArcCurve(
  start: THREE.Vector3,
  end: THREE.Vector3,
  radius: number,
  altitude: number = 0.15,
): THREE.CatmullRomCurve3 {
  const points: THREE.Vector3[] = [];
  const segments = 50;
  for (let i = 0; i <= segments; i++) {
    const t = i / segments;
    const point = new THREE.Vector3();
    point.copy(start).lerp(end, t);
    point.normalize();
    const altitudeFactor = Math.sin(t * Math.PI) * altitude;
    point.multiplyScalar(radius * (1 + altitudeFactor));
    points.push(point);
  }
  return new THREE.CatmullRomCurve3(points);
}

function createHorizonRing(
  radius: number,
  segments: number = 128,
): THREE.BufferGeometry {
  const points: THREE.Vector3[] = [];
  for (let i = 0; i <= segments; i++) {
    const angle = (i / segments) * Math.PI * 2;
    points.push(
      new THREE.Vector3(Math.cos(angle) * radius, 0, Math.sin(angle) * radius),
    );
  }
  return new THREE.BufferGeometry().setFromPoints(points);
}

export function AerospaceGlobeVisual({ isVisible }: AerospaceGlobeVisualProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const sceneRef = useRef<{
    scene: THREE.Scene;
    camera: THREE.PerspectiveCamera;
    renderer: THREE.WebGLRenderer;
    globe: THREE.Group;
    arcs: THREE.Group;
    animationId: number | null;
    startTime: number;
  } | null>(null);
  const [isInitialized, setIsInitialized] = useState(false);

  useEffect(() => {
    if (!containerRef.current || !isVisible) return;

    const prefersReducedMotion = window.matchMedia(
      "(prefers-reduced-motion: reduce)",
    ).matches;

    const container = containerRef.current;
    const width = container.clientWidth;
    const height = container.clientHeight;

    const computedStyle = getComputedStyle(document.documentElement);
    const accentColor =
      computedStyle.getPropertyValue("--color-accent-primary").trim() ||
      "#0055A4";
    const threeAccentColor = new THREE.Color(accentColor);

    const scene = new THREE.Scene();
    const camera = new THREE.PerspectiveCamera(45, width / height, 0.1, 1000);
    camera.position.z = 4.2;
    camera.position.y = 0.4;

    const renderer = new THREE.WebGLRenderer({
      alpha: true,
      antialias: true,
      powerPreference: "high-performance",
    });
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    renderer.setSize(width, height);
    container.appendChild(renderer.domElement);

    const globeRadius = 1.2;
    const globe = new THREE.Group();

    // Horizon rings
    const horizonRingMaterial = new THREE.ShaderMaterial({
      uniforms: {
        color: { value: threeAccentColor },
        opacity: { value: 0.1 },
      },
      vertexShader: `
        void main() {
          gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
        }
      `,
      fragmentShader: `
        uniform vec3 color;
        uniform float opacity;
        void main() {
          gl_FragColor = vec4(color, opacity);
        }
      `,
      transparent: true,
      blending: THREE.AdditiveBlending,
      depthWrite: false,
    });

    const ring1 = new THREE.Line(
      createHorizonRing(globeRadius),
      horizonRingMaterial.clone(),
    );
    ring1.rotation.x = Math.PI * 0.08;
    ring1.rotation.z = Math.PI * 0.05;
    globe.add(ring1);

    const ring2 = new THREE.Line(
      createHorizonRing(globeRadius),
      horizonRingMaterial.clone(),
    );
    ring2.rotation.x = Math.PI * 0.28;
    ring2.rotation.y = Math.PI * 0.15;
    globe.add(ring2);

    const ring3 = new THREE.Line(
      createHorizonRing(globeRadius),
      horizonRingMaterial.clone(),
    );
    ring3.rotation.x = -Math.PI * 0.22;
    ring3.rotation.y = Math.PI * 0.4;
    ring3.rotation.z = Math.PI * 0.1;
    globe.add(ring3);

    // Location nodes
    const nodeGeometry = new THREE.BufferGeometry();
    const nodePositions: number[] = [];
    const nodeSizes: number[] = [];

    locationNodes.forEach((node) => {
      const pos = latLonToVector3(node.lat, node.lon, globeRadius * 1.005);
      nodePositions.push(pos.x, pos.y, pos.z);
      nodeSizes.push(node.size);
    });

    nodeGeometry.setAttribute(
      "position",
      new THREE.Float32BufferAttribute(nodePositions, 3),
    );
    nodeGeometry.setAttribute(
      "size",
      new THREE.Float32BufferAttribute(nodeSizes, 1),
    );

    const nodeMaterial = new THREE.ShaderMaterial({
      uniforms: {
        time: { value: 0 },
        color: { value: threeAccentColor },
        pixelRatio: { value: renderer.getPixelRatio() },
      },
      vertexShader: `
        attribute float size;
        uniform float pixelRatio;
        varying float vSize;
        void main() {
          vSize = size;
          vec4 mvPosition = modelViewMatrix * vec4(position, 1.0);
          gl_PointSize = size * pixelRatio * (120.0 / -mvPosition.z);
          gl_Position = projectionMatrix * mvPosition;
        }
      `,
      fragmentShader: `
        uniform float time;
        uniform vec3 color;
        varying float vSize;
        void main() {
          vec2 center = gl_PointCoord - vec2(0.5);
          float dist = length(center);
          float core = smoothstep(0.5, 0.2, dist);
          float glow = smoothstep(0.5, 0.0, dist) * 0.3;
          float pulse = sin(time * 1.2 + vSize * 4.0) * 0.1 + 0.9;
          float alpha = (core * 0.85 + glow) * pulse;
          if (alpha < 0.02) discard;
          vec3 finalColor = color + vec3(0.1) * core;
          gl_FragColor = vec4(finalColor, alpha);
        }
      `,
      transparent: true,
      blending: THREE.AdditiveBlending,
      depthWrite: false,
    });

    const nodePoints = new THREE.Points(nodeGeometry, nodeMaterial);
    globe.add(nodePoints);

    // Flight arcs
    const arcs = new THREE.Group();

    flightConnections.forEach((conn, index) => {
      const startNode = locationNodes[conn.from];
      const endNode = locationNodes[conn.to];
      const startPos = latLonToVector3(
        startNode.lat,
        startNode.lon,
        globeRadius * 1.005,
      );
      const endPos = latLonToVector3(
        endNode.lat,
        endNode.lon,
        globeRadius * 1.005,
      );

      const curve = createArcCurve(startPos, endPos, globeRadius * 1.005, 0.1);
      const points = curve.getPoints(60);
      const arcGeometry = new THREE.BufferGeometry().setFromPoints(points);

      const progressAttr = new Float32Array(points.length);
      for (let i = 0; i < points.length; i++) {
        progressAttr[i] = i / (points.length - 1);
      }
      arcGeometry.setAttribute(
        "progress",
        new THREE.BufferAttribute(progressAttr, 1),
      );

      const arcMaterial = new THREE.ShaderMaterial({
        uniforms: {
          time: { value: 0 },
          color: { value: threeAccentColor },
          drawProgress: { value: 0 },
          arcIndex: { value: index },
          totalArcs: { value: flightConnections.length },
        },
        vertexShader: `
          attribute float progress;
          varying float vProgress;
          void main() {
            vProgress = progress;
            gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
          }
        `,
        fragmentShader: `
          uniform float time;
          uniform vec3 color;
          uniform float drawProgress;
          uniform float arcIndex;
          varying float vProgress;
          void main() {
            float drawEdge = smoothstep(drawProgress - 0.01, drawProgress, vProgress);
            if (drawEdge > 0.99) discard;
            float baseAlpha = 0.35;
            float travelSpeed = 0.25;
            float travelPos = mod(time * travelSpeed + arcIndex * 0.08, 1.0);
            float travelWidth = 0.18;
            float travel = smoothstep(travelWidth, 0.0, abs(vProgress - travelPos));
            float travelPos2 = mod(time * travelSpeed * 0.65 + arcIndex * 0.12 + 0.5, 1.0);
            float travel2 = smoothstep(travelWidth * 0.7, 0.0, abs(vProgress - travelPos2)) * 0.6;
            float edgeFade = smoothstep(0.0, 0.05, vProgress) * smoothstep(1.0, 0.95, vProgress);
            float alpha = (baseAlpha + travel * 0.5 + travel2 * 0.3) * edgeFade * (1.0 - drawEdge);
            vec3 finalColor = color + vec3(0.25, 0.3, 0.35) * (travel + travel2 * 0.5);
            gl_FragColor = vec4(finalColor, alpha);
          }
        `,
        transparent: true,
        blending: THREE.AdditiveBlending,
        depthWrite: false,
      });

      const arcLine = new THREE.Line(arcGeometry, arcMaterial);
      arcs.add(arcLine);
    });

    globe.add(arcs);

    // Outer glow
    const glowGeometry = new THREE.SphereGeometry(
      globeRadius * 1.06,
      32,
      32,
    );
    const glowMaterial = new THREE.ShaderMaterial({
      uniforms: {
        glowColor: { value: threeAccentColor },
        time: { value: 0 },
      },
      vertexShader: `
        varying vec3 vNormal;
        void main() {
          vNormal = normalize(normalMatrix * normal);
          gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
        }
      `,
      fragmentShader: `
        uniform vec3 glowColor;
        uniform float time;
        varying vec3 vNormal;
        void main() {
          float rim = pow(0.72 - dot(vNormal, vec3(0.0, 0.0, 1.0)), 4.0);
          gl_FragColor = vec4(glowColor, rim * 0.04);
        }
      `,
      side: THREE.BackSide,
      blending: THREE.AdditiveBlending,
      transparent: true,
      depthWrite: false,
    });
    const outerGlow = new THREE.Mesh(glowGeometry, glowMaterial);
    scene.add(outerGlow);

    scene.add(globe);

    sceneRef.current = {
      scene,
      camera,
      renderer,
      globe,
      arcs,
      animationId: null,
      startTime: performance.now(),
    };

    setIsInitialized(true);

    // Animation loop
    let time = 0;
    const animate = () => {
      if (!sceneRef.current) return;

      time += 0.016;
      const elapsed =
        (performance.now() - sceneRef.current.startTime) / 1000;

      const {
        camera: cam,
        renderer: rend,
        scene: sc,
        globe: gl,
        arcs: ar,
      } = sceneRef.current;

      (nodeMaterial as THREE.ShaderMaterial).uniforms.time.value = time;
      (glowMaterial as THREE.ShaderMaterial).uniforms.time.value = time;

      ar.children.forEach((arc, index) => {
        const material = (arc as THREE.Line).material as THREE.ShaderMaterial;
        material.uniforms.time.value = time;
        const drawDelay = 0.5 + index * 0.06;
        const drawDuration = 1.0;
        const drawProgress = Math.min(
          1,
          Math.max(0, (elapsed - drawDelay) / drawDuration),
        );
        material.uniforms.drawProgress.value = drawProgress;
      });

      if (!prefersReducedMotion) {
        gl.rotation.y += 0.002;
        gl.rotation.x = Math.sin(time * 0.08) * 0.02;
        cam.position.x = Math.sin(time * 0.04) * 0.1;
        cam.position.y = 0.4 + Math.cos(time * 0.03) * 0.06;
        cam.lookAt(0, 0, 0);
      }

      rend.render(sc, cam);
      sceneRef.current.animationId = requestAnimationFrame(animate);
    };

    animate();

    const handleResize = () => {
      if (!containerRef.current || !sceneRef.current) return;
      const w = containerRef.current.clientWidth;
      const h = containerRef.current.clientHeight;
      sceneRef.current.camera.aspect = w / h;
      sceneRef.current.camera.updateProjectionMatrix();
      sceneRef.current.renderer.setSize(w, h);
    };

    window.addEventListener("resize", handleResize);

    return () => {
      window.removeEventListener("resize", handleResize);
      if (sceneRef.current) {
        if (sceneRef.current.animationId !== null) {
          cancelAnimationFrame(sceneRef.current.animationId);
        }
        sceneRef.current.renderer.dispose();
        globe.traverse((child) => {
          if (
            child instanceof THREE.Mesh ||
            child instanceof THREE.Points ||
            child instanceof THREE.Line
          ) {
            child.geometry.dispose();
            if (Array.isArray(child.material)) {
              child.material.forEach((m) => m.dispose());
            } else {
              child.material.dispose();
            }
          }
        });
        if (container.contains(sceneRef.current.renderer.domElement)) {
          container.removeChild(sceneRef.current.renderer.domElement);
        }
      }
    };
  }, [isVisible]);

  return (
    <div
      ref={containerRef}
      className={`relative w-full aspect-square transition-opacity duration-1000 ${
        isVisible && isInitialized ? "opacity-100" : "opacity-0"
      }`}
      style={{
        minHeight: "400px",
        maxHeight: "600px",
      }}
    />
  );
}
