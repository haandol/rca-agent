import * as fs from 'fs';
import * as path from 'path';
import ts from 'typescript';

test('production sends analysis completion to the notification topic', () => {
  const fileName = path.resolve(__dirname, '../bin/infra.ts');
  const source = fs.readFileSync(fileName, 'utf8');
  const sourceFile = ts.createSourceFile(
    fileName,
    source,
    ts.ScriptTarget.Latest,
    true,
    ts.ScriptKind.TS,
  );
  const notificationTopicInitializers: string[] = [];

  function visit(node: ts.Node): void {
    if (
      ts.isPropertyAssignment(node) &&
      node.name.getText(sourceFile) === 'notificationTopic'
    ) {
      notificationTopicInitializers.push(node.initializer.getText(sourceFile));
    }
    ts.forEachChild(node, visit);
  }
  visit(sourceFile);

  expect(notificationTopicInitializers).toEqual([
    'eventBusStack.notificationTopic',
    'eventBusStack.notificationTopic',
  ]);
});
