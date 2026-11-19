export default function getAvatar(name = "Agent") {
  const hue = [...name].reduce((sum, char) => sum + char.charCodeAt(0), 0) % 360;
  const label = name.replace(/[^A-Za-z0-9]/g, "").slice(0, 2).toUpperCase() || "A";
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="48" height="48"><rect width="48" height="48" rx="24" fill="hsl(${hue},45%,40%)"/><text x="24" y="30" text-anchor="middle" fill="white" font-family="sans-serif" font-size="17">${label}</text></svg>`;
  return `data:image/svg+xml,${encodeURIComponent(svg)}`;
}
