// Synthetic, in-memory Atlas for Eidolon's native HTTP contract tests.
// The sibling path is supplied explicitly; this never opens its database or index.js.
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';

const sibling = process.argv[2];
const { Atlas } = await import(pathToFileURL(join(sibling, 'src/atlas.js')));
const { createServer, listen } = await import(pathToFileURL(join(sibling, 'src/server.js')));
const atlas = new Atlas({ databasePath: ':memory:' });
const server = createServer({ atlas });

function pdf(text) {
  const stream = `BT /F1 12 Tf 20 50 Td (${text}) Tj ET`;
  const objects = [
    '<< /Type /Catalog /Pages 2 0 R >>',
    '<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
    '<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 100] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>',
    '<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
    `<< /Length ${Buffer.byteLength(stream)} >>\nstream\n${stream}\nendstream`,
  ];
  let content = '%PDF-1.4\n';
  const offsets = [0];
  for (const [index, object] of objects.entries()) {
    offsets.push(Buffer.byteLength(content));
    content += `${index + 1} 0 obj\n${object}\nendobj\n`;
  }
  const xref = Buffer.byteLength(content);
  content += `xref\n0 ${objects.length + 1}\n0000000000 65535 f \n`;
  content += offsets.slice(1).map(offset => `${String(offset).padStart(10, '0')} 00000 n \n`).join('');
  content += `trailer\n<< /Size ${objects.length + 1} /Root 1 0 R >>\nstartxref\n${xref}\n%%EOF\n`;
  return Buffer.from(content);
}

try {
  await atlas.setup('synthetic native read contract passphrase');
  const provenance = { attribution: 'stated', evidence: [{ source: 'Synthetic fixture', excerpt: 'Test only' }] };
  const field = atlas.createCustomField({ category: 'person', type: 'number', name: 'Precise test number' });
  const person = atlas.createRecord({ category: 'person', title: 'Synthetic Person', data: {
    preferredName: 'Fixture', pronouns: 'they/them', birthDate: '2000-01', currentCity: 'Test City',
    currentRegion: 'Test Region', currentCountry: 'Canada', timeZone: 'America/Toronto',
    occupation: 'Test engineer', organization: 'Synthetic Org', languages: ['English'],
    languageProficiencies: { English: 'native' }, passportNumber: 'SYNTHETIC-ID-NOT-A-REAL-PASSPORT',
    physicalHealth: { measurements: [{ id: 'weight', kind: 'weightKg', value: 70.123456789,
      measuredOn: '2026-01-01', source: 'Synthetic measurement' }], allergyStatus: 'none_known' },
    provenance,
  }, customFieldValues: { [field.id]: 0.12345678912345 } });
  atlas.patchRecord(person.id, { revision: person.revision, data: { ...person.data, notes: 'Second synthetic revision' } });
  const interest = atlas.createRecord({ category: 'interest', title: 'Synthetic photography',
    data: { liking: 85.123456789, skillLevel: 44.5, provenance } });
  const experience = atlas.createRecord({ category: 'experience', title: 'Synthetic trip',
    data: { kind: 'event', startDate: '2026-01-01', narrative: 'Synthetic story', provenance } });
  const project = atlas.createRecord({ category: 'project', title: 'Synthetic project',
    data: { context: 'Synthetic context', status: 'active', githubLink: 'https://example.test/repository', provenance } });
  const relationship = atlas.createRecord({ category: 'relationship', title: 'Synthetic friend',
    data: { kind: 'friend', notes: 'Synthetic notes', provenance } });
  const resource = atlas.createRecord({ category: 'resource', title: 'Synthetic tool',
    data: { resourceVersion: 1, kind: 'tool', quantity: 1.234567891, toolType: 'software', notes: 'Synthetic only', provenance } });
  const end = atlas.createRecord({ category: 'end', title: 'Synthetic curiosity',
    data: { endCategory: 'interests', status: 'active', description: 'Understand synthetic systems', provenance } });
  const belief = atlas.createRecord({ category: 'belief', title: 'Synthetic belief',
    data: { domain: 'world', status: 'held', description: 'Practice supports learning', believedCertainty: 0.87654321, provenance } });
  const personality = atlas.createRecord({ category: 'personality', title: 'Personality',
    data: { spectra: [{ key: 'sociability', score: 12.3456789, confidence: 0.7654321, description: 'Synthetic spectrum' }], provenance } });
  const goal = atlas.createRecord({ category: 'goal', title: 'Synthetic fractional goal', data: {
    description: 'Synthetic work', successConditions: 'Contract passes', pursuitStatus: 'active', commitment: 77.7, provenance,
    blueprint: { nodes: [{ id: 'draft', title: 'Draft', progress: 12.5 }, { id: 'check', title: 'Check', progress: 50.25 }],
      edges: [{ sourceId: 'draft', targetId: 'check' }] },
  } });
  const emptyGoal = atlas.createRecord({ category: 'goal', title: 'Synthetic unknown goal', data: {} });
  const link = atlas.createLink({ sourceId: goal.id, targetId: end.id, type: 'SERVES',
    beliefIds: [belief.id], degreePercent: 66.6666667, provenance });
  atlas.patchLink(link.id, { revision: link.revision, notes: 'Second synthetic link revision' });
  atlas.createLink({ sourceId: project.id, targetId: resource.id, type: 'related', notes: 'Synthetic connection' });
  const root = atlas.createKnowledgeNode({ name: 'Synthetic contract subject', branch: 'subjects',
    status: 'known', understanding: 'A synthetic explanation', terms: [{ id: 'fixture', label: 'Fixture', definition: 'Synthetic data' }] });
  const child = atlas.createKnowledgeNode({ name: 'Synthetic child', branch: 'subjects', parentId: root.id, status: 'unassessed' });
  atlas.createKnowledgeConnection({ sourceId: root.id, targetId: child.id });
  atlas.registerStateObservation({ eventId: 'synthetic-manual-estimate',
    states: { mental_energy: { value: 0.456789123, confidence: 0.87654321 } } });
  const resumeBytes = pdf('Synthetic resume text for native contract');
  atlas.putResume(person.id, { filename: 'synthetic.pdf', mimeType: 'application/pdf', bytes: resumeBytes });
  const imageBytes = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jKX0AAAAASUVORK5CYII=', 'base64');
  const image = atlas.createImage(experience.id, { filename: 'synthetic.png', mimeType: 'image/png', bytes: imageBytes });
  const { port } = await listen(server, { port: 0 });
  process.stdout.write(`${JSON.stringify({ port, at: new Date().toISOString(), ids: {
    person: person.id, field: field.id, interest: interest.id, experience: experience.id, project: project.id,
    relationship: relationship.id, resource: resource.id, end: end.id, belief: belief.id,
    personality: personality.id, goal: goal.id, emptyGoal: emptyGoal.id, link: link.id,
    knowledgeRoot: root.id, knowledgeChild: child.id, image: image.id,
  }, resume: resumeBytes.toString('base64'), image: imageBytes.toString('base64') })}\n`);
  const close = () => server.close(() => process.exit(0));
  process.stdin.once('data', close);
  process.stdin.once('end', close);
  process.once('SIGTERM', close);
} catch (error) {
  atlas.close();
  throw error;
}
